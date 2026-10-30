"""Bounded private native thumbnails from existing media, never caller-selected URLs."""

from __future__ import annotations

import base64
import binascii
import io
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import cv2
from PIL import Image, UnidentifiedImageError

from frigate.const import CLIPS_DIR, THUMB_DIR
from frigate.models import Event, ReviewSegment
from frigate.output.preview import PREVIEW_CACHE_DIR, get_most_recent_preview_frame

if TYPE_CHECKING:
    from frigate.track.object_processing import TrackedObjectProcessor

MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_SOURCE_PIXELS = 16 * 1024 * 1024
_RENDER_SLOTS = threading.BoundedSemaphore(2)


class NativeThumbnailUnavailable(ValueError):
    """A missing, corrupt, unsafe or oversized private source."""


@dataclass(frozen=True)
class NativeThumbnail:
    """An ID-resolved camera and source, authorized before media is read."""

    kind: Literal["review", "event", "camera"]
    identifier: str
    camera: str
    path: str | None = None
    embedded: str | None = None


def resolve_native_thumbnail(
    kind: Literal["review", "event", "camera"], identifier: str
) -> NativeThumbnail:
    """Resolve existing database references without reading image data."""
    if kind == "camera":
        return NativeThumbnail(kind, identifier, identifier)
    if kind == "review":
        review = ReviewSegment.get_or_none(ReviewSegment.id == identifier)
        if review is None:
            raise NativeThumbnailUnavailable("Review thumbnail is unavailable")
        if (review.data or {}).get("native_person_best_shot") is False:
            raise NativeThumbnailUnavailable(
                "A completed native person best shot is unavailable"
            )
        return NativeThumbnail(kind, identifier, review.camera, path=review.thumb_path)
    event = Event.get_or_none(Event.id == identifier)
    if event is None:
        raise NativeThumbnailUnavailable("Event thumbnail is unavailable")
    return NativeThumbnail(kind, identifier, event.camera, embedded=event.thumbnail)


def _read_private_image(path: Path, directory: Path) -> bytes:
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(directory.resolve()) or not resolved.is_file():
        raise NativeThumbnailUnavailable(
            "Thumbnail source is outside the private media directory"
        )
    with resolved.open("rb") as source:
        value = source.read(MAX_IMAGE_BYTES + 1)
    if not value or len(value) > MAX_IMAGE_BYTES:
        raise NativeThumbnailUnavailable("Thumbnail source size is invalid")
    return value


def render_native_thumbnail(
    reference: NativeThumbnail,
    frame_processor: TrackedObjectProcessor | None,
    height: int,
) -> bytes:
    """Return a small metadata-free JPEG without starting recording/video decoders."""
    if not _RENDER_SLOTS.acquire(blocking=False):
        raise NativeThumbnailUnavailable("Native thumbnail renderer is busy")
    try:
        return _render(reference, frame_processor, height)
    finally:
        _RENDER_SLOTS.release()


def _render(
    reference: NativeThumbnail,
    frame_processor: TrackedObjectProcessor | None,
    height: int,
) -> bytes:
    try:
        if reference.kind == "camera":
            frame = (
                frame_processor.get_current_frame(reference.camera, {})
                if frame_processor is not None
                else None
            )
            if (
                frame_processor is not None
                and time.time()
                - frame_processor.get_current_frame_time(reference.camera)
                > 10
            ):
                frame = None
            if frame is not None:
                if (
                    frame.ndim != 3
                    or frame.shape[2] != 3
                    or frame.shape[0] * frame.shape[1] > MAX_SOURCE_PIXELS
                ):
                    raise NativeThumbnailUnavailable(
                        "Camera frame dimensions are invalid"
                    )
                image = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                return _encode(image, height)
            preview = get_most_recent_preview_frame(reference.camera)
            if preview is None:
                raise NativeThumbnailUnavailable(
                    "No existing camera preview is available"
                )
            value = _read_private_image(Path(preview), Path(PREVIEW_CACHE_DIR))
        elif reference.kind == "review":
            if not reference.path:
                raise NativeThumbnailUnavailable("No review preview is available")
            value = _read_private_image(
                Path(reference.path), Path(CLIPS_DIR) / "review"
            )
        elif reference.embedded:
            if len(reference.embedded) > ((MAX_IMAGE_BYTES + 2) // 3) * 4:
                raise NativeThumbnailUnavailable("Event preview is too large")
            value = base64.b64decode(reference.embedded, validate=True)
            if not value or len(value) > MAX_IMAGE_BYTES:
                raise NativeThumbnailUnavailable("Event preview size is invalid")
        else:
            value = _read_private_image(
                Path(THUMB_DIR) / reference.camera / f"{reference.identifier}.webp",
                Path(THUMB_DIR),
            )
        with Image.open(io.BytesIO(value)) as image:
            if (
                image.format not in ("JPEG", "PNG", "WEBP")
                or image.width * image.height > MAX_SOURCE_PIXELS
            ):
                raise NativeThumbnailUnavailable(
                    "Thumbnail format or dimensions are invalid"
                )
            return _encode(image, height)
    except (
        OSError,
        ValueError,
        TypeError,
        binascii.Error,
        UnidentifiedImageError,
        Image.DecompressionBombError,
        cv2.error,
    ) as error:
        raise NativeThumbnailUnavailable("Private thumbnail is unavailable") from error


def _encode(image: Image.Image, height: int) -> bytes:
    image.thumbnail((1024, height))
    output = io.BytesIO()
    image.convert("RGB").save(output, format="JPEG", quality=75)
    value = output.getvalue()
    if len(value) > MAX_IMAGE_BYTES:
        raise NativeThumbnailUnavailable("Rendered thumbnail is too large")
    return value
