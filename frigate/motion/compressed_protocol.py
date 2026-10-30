"""Bounded encoded-packet/motion transport from the entropy-only extractor, never image decoding."""

import struct
from collections.abc import Iterator
from fractions import Fraction
from typing import IO, Annotated, Literal, Self, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from frigate.motion.compressed_buffer import EncodedPacket, H264Stream, h264_nal_types
from frigate.motion.compressed_mux import avc_parameter_sets

HEADER = struct.Struct("<4scI")
PACKET = struct.Struct("<qqqIQ")
MAX_RECORD_BYTES = 32 * 1024**2
GRID_CELLS = 18 * 32
Score: TypeAlias = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]


class StreamHeader(BaseModel):
    """Source configuration only; the extractor may not know geometry until its first frame."""

    model_config = ConfigDict(extra="forbid")
    codec: Literal["h264"]
    mode: Literal["entropy_only"]
    width: int = Field(ge=0, le=16384)
    height: int = Field(ge=0, le=16384)
    time_base: tuple[int, int]
    extradata_hex: str = Field(max_length=131072)

    @field_validator("time_base")
    @classmethod
    def positive_timebase(cls, value: tuple[int, int]) -> tuple[int, int]:
        """Require an actual rational time base rather than guessing frame arrival time."""
        if value[0] <= 0 or value[1] <= 0:
            raise ValueError("Invalid extractor time base")
        return value

    def native_stream(self, width: int, height: int) -> H264Stream:
        """Use verified native geometry, rejecting advertised/actual configuration drift."""
        if (self.width and self.width != width) or (
            self.height and self.height != height
        ):
            raise ValueError("Extractor native geometry changed")
        extra = bytes.fromhex(self.extradata_hex)
        length_size = None
        if extra:
            if extra[0] == 1:
                _, length_size = avc_parameter_sets(extra)
            elif not {7, 8} <= set(h264_nal_types(extra)):
                raise ValueError("Extractor codec configuration lacks parameter sets")
        return H264Stream(width, height, extra, length_size, Fraction(*self.time_base))


class CompressedEvidence(BaseModel):
    """Predictive and non-skipped-coded-block evidence, not a claim of physical motion."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    type: Literal["frame"]
    index: int = Field(ge=0)
    pts: int
    relative_seconds: float = Field(ge=0)
    width: int = Field(ge=1, le=16384)
    height: int = Field(ge=1, le=16384)
    picture_type: Literal["I", "P", "B", "?"]
    vector_count: int = Field(ge=0, le=2000000)
    moving_vectors: int = Field(ge=0, le=2000000)
    coverage: Score
    vectors_sha256: str | None
    uncertainty: str | None
    time_base: tuple[int, int]
    profile_idc: int
    cabac: Literal[0, 1]
    activity: list[Score] = Field(min_length=GRID_CELLS, max_length=GRID_CELLS)
    coded_activity: list[Score] = Field(min_length=GRID_CELLS, max_length=GRID_CELLS)
    vector_x: list[Annotated[float, Field(allow_inf_nan=False)]] | None = Field(
        default=None, min_length=GRID_CELLS, max_length=GRID_CELLS
    )
    vector_y: list[Annotated[float, Field(allow_inf_nan=False)]] | None = Field(
        default=None, min_length=GRID_CELLS, max_length=GRID_CELLS
    )

    @model_validator(mode="after")
    def paired_vector_directions(self) -> Self:
        """Never invent a direction when one or both vector components are unavailable."""
        if (self.vector_x is None) != (self.vector_y is None):
            raise ValueError("Both native vector direction components are required")
        return self

    @field_validator("pts")
    @classmethod
    def require_pts(cls, value: int) -> int:
        """Reject FFmpeg's unavailable-timestamp sentinel."""
        if value == -(1 << 63):
            raise ValueError("Extractor presentation time is unavailable")
        return value

    @field_validator("time_base")
    @classmethod
    def positive_timebase(cls, value: tuple[int, int]) -> tuple[int, int]:
        """Validate the source clock independently of the transport header."""
        return StreamHeader.positive_timebase(value)

    @property
    def timestamp(self) -> float:
        """Return source presentation time without changing encoded packet ticks."""
        return float(self.pts * Fraction(*self.time_base))


class ExtractorSummary(BaseModel):
    """Only an instrumented no-reconstruction run can report successful completion."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    type: Literal["summary"]
    mode: Literal["entropy_only"]
    frames: int = Field(gt=0)
    vector_frames: int = Field(gt=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    profile_idc: int
    cabac: Literal[0, 1]
    parsed_macroblocks: int = Field(gt=0)
    reconstructed_macroblocks: Literal[0]
    deblocked_rows: Literal[0]
    pixel_reconstruction_performed: Literal[False]
    frame_buffers_allocated: Literal[True]
    vectors_hashed: bool
    cpu_seconds: float = Field(ge=0)
    wall_seconds: float = Field(ge=0)


class PacketRecord:
    """Original encoded access-unit bytes, flags and integer source timestamps."""

    def __init__(self, payload: bytes) -> None:
        if len(payload) <= PACKET.size:
            raise ValueError("Truncated extractor packet")
        self.pts, self.dts, self.duration, self.flags, self.index = PACKET.unpack_from(
            payload
        )
        if self.pts == -(1 << 63) or self.dts == -(1 << 63):
            raise ValueError("Encoded packet timestamp is unavailable")
        if self.duration < 0 or self.flags & 2:
            raise ValueError("Corrupt extractor packet metadata")
        self.data = payload[PACKET.size :]

    def for_stream(self, stream: H264Stream) -> EncodedPacket:
        """Attach verified codec geometry without changing any coded slice or timestamp."""
        return EncodedPacket(
            stream, self.data, self.dts, self.pts, self.duration, self.flags
        )


ExtractorEvent: TypeAlias = (
    StreamHeader | CompressedEvidence | ExtractorSummary | PacketRecord
)


def _read_exact(handle: IO[bytes], size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = handle.read(size - len(chunks))
        if not chunk:
            raise EOFError("Truncated entropy extractor record")
        chunks.extend(chunk)
    return bytes(chunks)


def read_extractor_events(handle: IO[bytes]) -> Iterator[ExtractorEvent]:
    """Read bounded private binary records; missing evidence is never a quiet-motion result."""
    configured = False
    finished = False
    header_config: StreamHeader | None = None
    packet_index = 0
    frame_index = 0
    geometry: tuple[int, int] | None = None
    while True:
        first = handle.read(1)
        if not first:
            if not finished:
                raise EOFError("Extractor ended without a verified summary")
            return
        magic, kind, length = HEADER.unpack(
            first + _read_exact(handle, HEADER.size - 1)
        )
        if magic != b"FCM1" or not 0 < length <= MAX_RECORD_BYTES:
            raise ValueError("Invalid entropy extractor record header")
        if finished:
            raise ValueError("Extractor emitted data after its completion record")
        if kind not in (b"C", b"P", b"M", b"S"):
            raise ValueError("Unknown entropy extractor record type")
        payload = _read_exact(handle, length)
        if kind == b"C":
            if configured:
                raise ValueError(
                    "Extractor codec configuration changed without restart"
                )
            event = StreamHeader.model_validate_json(payload)
            header_config = event
            configured = True
            yield event
            continue
        if not configured:
            raise ValueError("Extractor packet/evidence preceded codec configuration")
        if kind == b"P":
            packet = PacketRecord(payload)
            if packet.index != packet_index:
                raise ValueError("Encoded access-unit transport lost its sequence")
            packet_index += 1
            yield packet
        elif kind == b"M":
            evidence = CompressedEvidence.model_validate_json(payload)
            if evidence.index != frame_index:
                raise ValueError("Compressed evidence transport lost its sequence")
            if header_config is None or evidence.time_base != header_config.time_base:
                raise ValueError("Compressed evidence changed the source time base")
            current_geometry = (evidence.width, evidence.height)
            if geometry is not None and current_geometry != geometry:
                raise ValueError("Compressed evidence changed native geometry")
            if (header_config.width and header_config.width != evidence.width) or (
                header_config.height and header_config.height != evidence.height
            ):
                raise ValueError("Compressed evidence conflicts with source geometry")
            geometry = current_geometry
            frame_index += 1
            yield evidence
        else:
            summary = ExtractorSummary.model_validate_json(payload)
            if summary.frames != frame_index:
                raise ValueError(
                    "Extractor summary did not account for observed frames"
                )
            yield summary
            finished = True
