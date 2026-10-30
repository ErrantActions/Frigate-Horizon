from typing import Literal

from pydantic import BaseModel, Field
from pydantic.json_schema import SkipJsonSchema

from frigate.record.export import (
    ChaptersEnum,
    ExportStreamEnum,
    PlaybackSourceEnum,
)
from frigate.record.export_metadata import ExportMetadataCategory
from frigate.record.export_timestamps import ExportTimestampMode


class ExportRecordingsBody(BaseModel):
    metadata_categories: list[ExportMetadataCategory] = Field(
        default_factory=list,
        max_length=7,
        description="Explicitly selected investigation context embedded in the exported MP4 comment. Empty by default. May include sensitive identities; unavailable context is labeled.",
    )
    timestamp: ExportTimestampMode = Field(
        default=ExportTimestampMode.none,
        description="Optional capture-timeline labels: subtitle, burn_in, or both. Realtime recording exports only; source recordings are unchanged.",
    )
    source: PlaybackSourceEnum = Field(
        default=PlaybackSourceEnum.recordings, title="Playback source"
    )
    name: str | None = Field(title="Friendly name", default=None, max_length=256)
    image_path: str | SkipJsonSchema[None] = None
    export_case_id: str | None = Field(
        default=None,
        title="Export case ID",
        max_length=30,
        description="ID of the export case to assign this export to",
    )
    chapters: ChaptersEnum | None = Field(
        default=None,
        title="Chapter mode",
        description=(
            "Optional chapter metadata to embed in the export. When omitted, "
            "the camera's configured export chapter mode is used."
        ),
    )
    stream: ExportStreamEnum = Field(
        default=ExportStreamEnum.auto,
        title="Recorded stream to export",
        description=(
            "Which recorded stream to export. 'auto' uses the merged "
            "timeline, preferring the main stream and falling back to the "
            "sub stream where main has aged out. 'main' or 'sub' pins the "
            "export to that stream alone."
        ),
    )


class ExportRecordingsCustomBody(BaseModel):
    timestamp: Literal[ExportTimestampMode.none] = Field(
        default=ExportTimestampMode.none,
        description="Custom-speed timestamp mapping is not certified; only none is accepted.",
    )
    metadata_categories: list[ExportMetadataCategory] = Field(
        default_factory=list,
        max_length=0,
        description="Investigation context is supported on realtime recording exports only.",
    )
    source: PlaybackSourceEnum = Field(
        default=PlaybackSourceEnum.recordings, title="Playback source"
    )
    name: str = Field(title="Friendly name", default=None, max_length=256)
    image_path: str | SkipJsonSchema[None] = None
    export_case_id: str | None = Field(
        default=None,
        title="Export case ID",
        max_length=30,
        description="ID of the export case to assign this export to",
    )
    ffmpeg_input_args: str | None = Field(
        default=None,
        title="FFmpeg input arguments",
        description="Custom FFmpeg input arguments. If not provided, defaults to timelapse input args.",
    )
    ffmpeg_output_args: str | None = Field(
        default=None,
        title="FFmpeg output arguments",
        description="Custom FFmpeg output arguments. If not provided, defaults to timelapse output args.",
    )
    cpu_fallback: bool = Field(
        default=False,
        title="CPU Fallback",
        description="If true, retry export without hardware acceleration if the initial export fails.",
    )
