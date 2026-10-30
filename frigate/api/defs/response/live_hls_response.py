"""Public live HLS metadata without private packager URLs or sessions."""

from typing import Literal

from pydantic import BaseModel


class LiveHlsRenditionInfo(BaseModel):
    height: int
    bitrate: int


class LiveHlsStatus(BaseModel):
    version: Literal[1] = 1
    protocol: Literal["ll-hls"] = "ll-hls"
    enabled: bool
    available: bool
    playlist: str | None
    renditions: list[LiveHlsRenditionInfo]
