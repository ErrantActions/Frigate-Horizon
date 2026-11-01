"""On-demand WebRTC viewing and authenticated local TURN relay settings."""

import ipaddress
import re
from pathlib import PurePosixPath
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from .base import FrigateBaseModel


class LiveTurnConfig(FrigateBaseModel):
    enabled: bool = Field(
        default=False,
        title="Enable authenticated TURN",
        description="Run Coturn inside Frigate for authorized WebRTC viewers. Requires explicit firewall and address configuration.",
    )
    hostname: str = Field(
        default="",
        title="TURN hostname",
        description="DNS-only hostname or IP reachable by remote viewers. Do not include a scheme or port.",
    )
    listen_ip: str = Field(
        default="0.0.0.0",
        title="TURN listening IP",
        description="Local IPv4 interface for the relay listeners.",
    )
    relay_ip: str | None = Field(
        default=None,
        title="TURN local relay IPv4",
        description="Local container/LAN IPv4 that owns the relay UDP sockets. Required when TURN is enabled.",
    )
    external_ip: str | None = Field(
        default=None,
        title="TURN public IPv4",
        description="Public IPv4 advertised by a relay behind NAT. Forward the configured relay UDP range unchanged.",
    )
    port: int = Field(default=3478, ge=1024, le=65535, title="TURN UDP/TCP port")
    tls_port: int = Field(default=5349, ge=1024, le=65535, title="TURN TLS port")
    tls: bool = Field(
        default=True,
        title="Enable TURN TLS",
        description="Use a valid certificate for the TURN hostname. Plain TURN is suitable only when explicitly selected; its media remains protected by WebRTC SRTP.",
    )
    certificate: str | None = Field(default=None, title="TURN certificate file")
    private_key: str | None = Field(default=None, title="TURN private key file")
    min_port: int = Field(
        default=49160, ge=1024, le=65535, title="First relay UDP port"
    )
    max_port: int = Field(default=49259, ge=1024, le=65535, title="Last relay UDP port")
    credential_seconds: int = Field(
        default=120, ge=60, le=600, title="Temporary TURN credential lifetime"
    )
    max_allocations: int = Field(
        default=128, ge=1, le=1024, title="Maximum TURN allocations"
    )

    @field_validator("listen_ip", "relay_ip", "external_ip")
    @classmethod
    def ipv4_only(cls, value: str | None) -> str | None:
        """Accept explicit IPv4 literals rather than ambiguous bind expressions."""
        return str(ipaddress.IPv4Address(value)) if value is not None else None

    @field_validator("hostname")
    @classmethod
    def valid_hostname(cls, value: str) -> str:
        """Keep relay URI construction independent of untrusted delimiters."""
        if value and (
            len(value) > 253
            or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", value)
            or ".." in value
        ):
            raise ValueError("TURN hostname must be a DNS hostname or IPv4 address")
        return value

    @field_validator("certificate", "private_key")
    @classmethod
    def absolute_file(cls, value: str | None) -> str | None:
        """Reject relative, multiline, and configuration-injection file paths."""
        if value is not None and (
            not value.startswith("/")
            or ".." in PurePosixPath(value).parts
            or any(character in value for character in "\r\n\x00")
        ):
            raise ValueError("TURN TLS files must use absolute container paths")
        return value

    @model_validator(mode="after")
    def coherent_relay(self) -> Self:
        """Require complete TLS/NAT settings only when the relay is enabled."""
        if self.min_port > self.max_port:
            raise ValueError("TURN relay port range is reversed")
        if self.port == self.tls_port or any(
            self.min_port <= port <= self.max_port
            for port in (self.port, self.tls_port)
        ):
            raise ValueError("TURN listener and relay ports must not overlap")
        if self.enabled and not self.hostname:
            raise ValueError("Enabled TURN requires a hostname reachable by viewers")
        if self.enabled and (
            self.relay_ip is None
            or ipaddress.IPv4Address(self.relay_ip).is_unspecified
            or ipaddress.IPv4Address(self.relay_ip).is_loopback
        ):
            raise ValueError("Enabled TURN requires an explicit local relay IPv4")
        if self.enabled and self.tls and not (self.certificate and self.private_key):
            raise ValueError("TURN TLS requires certificate and private key paths")
        return self


class LiveWebRTCConfig(FrigateBaseModel):
    enabled: bool = Field(
        default=False,
        title="Enable on-demand WebRTC viewing",
        description="Use supervised OvenMediaEngine for live and recorded viewing. Stored recordings retain their original source media. Requires restart after changes.",
    )
    advertised_hosts: list[str] = Field(
        default_factory=list,
        max_length=4,
        title="WebRTC media addresses",
        description="Explicit LAN and public IPv4 addresses reachable by viewers. Never use wildcard or container bridge addresses.",
    )
    udp_port: int = Field(
        default=8189, ge=1024, le=65535, title="WebRTC UDP media port"
    )
    max_viewers: int = Field(default=32, ge=1, le=128, title="Maximum viewing sessions")
    max_grid_cameras: int = Field(
        default=12, ge=1, le=64, title="Maximum active grid camera pipelines"
    )
    max_detail_cameras: int = Field(
        default=2, ge=1, le=8, title="Maximum active detailed viewing pipelines"
    )
    grid_height: Literal[360, 720] = Field(default=360, title="Grid video height")
    grid_fps: int = Field(default=10, ge=5, le=15, title="Grid video frames per second")
    detail_fps: int = Field(
        default=25, ge=10, le=30, title="Detailed video frames per second"
    )
    max_height: int = Field(
        default=1080,
        title="Maximum detailed video height",
        description="720, 1080, or 2160. The current open-source media engine uses CPU encoding; enable 2160 only after measuring load.",
    )
    encoder_threads: int = Field(
        default=2, ge=1, le=8, title="Threads per viewing video encoder"
    )
    lease_seconds: int = Field(
        default=30, ge=15, le=60, title="Viewer heartbeat timeout"
    )
    max_session_seconds: int = Field(
        default=3600, ge=60, le=28800, title="Maximum viewer session lifetime"
    )
    turn: LiveTurnConfig = Field(
        default_factory=LiveTurnConfig, title="Local TURN relay"
    )

    @field_validator("advertised_hosts")
    @classmethod
    def explicit_media_addresses(cls, values: list[str]) -> list[str]:
        """Advertise only operator-selected routable IPv4 candidates."""
        result = []
        for value in values:
            address = ipaddress.IPv4Address(value)
            if address.is_unspecified or address.is_multicast or address.is_loopback:
                raise ValueError(
                    "WebRTC addresses must be explicit reachable IPv4 addresses"
                )
            canonical = str(address)
            if canonical not in result:
                result.append(canonical)
        return result

    @field_validator("max_height")
    @classmethod
    def supported_height(cls, value: int) -> int:
        """Bound applications to the qualified viewing ladders."""
        if value not in (720, 1080, 2160):
            raise ValueError("Maximum WebRTC height must be 720, 1080, or 2160")
        return value

    @model_validator(mode="after")
    def coherent_media(self) -> Self:
        """Fail closed when enabled without a usable media candidate."""
        if self.enabled and not self.advertised_hosts:
            raise ValueError("Enabled WebRTC requires explicit media addresses")
        if self.turn.enabled and not self.enabled:
            raise ValueError("TURN requires WebRTC live viewing to be enabled")
        if self.udp_port in (3333, 8081) or (
            self.turn.enabled
            and (
                self.udp_port in (self.turn.port, self.turn.tls_port)
                or self.turn.min_port <= self.udp_port <= self.turn.max_port
            )
        ):
            raise ValueError("WebRTC and TURN service ports must not overlap")
        return self
