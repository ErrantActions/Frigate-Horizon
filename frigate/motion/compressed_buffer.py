"""Bounded native H264 pre-roll storage; never reconstructs or transforms pixels."""

import math
from collections import deque
from dataclasses import dataclass
from fractions import Fraction


class PrerollUnavailable(RuntimeError):
    """A decoder cannot safely start from the currently buffered encoded packets."""


def h264_nal_types(data: bytes, length_size: int | None = None) -> tuple[int, ...]:
    """Validate elementary NAL headers without reading motion syntax or image planes."""
    if not data:
        raise ValueError("Empty encoded packet")
    headers = []
    if length_size is not None:
        if length_size not in (1, 2, 3, 4):
            raise ValueError("Invalid AVC length-prefix size")
        offset = 0
        while offset < len(data):
            if offset + length_size > len(data):
                raise ValueError("Truncated AVC length prefix")
            size = int.from_bytes(data[offset : offset + length_size], "big")
            offset += length_size
            if size < 1 or offset + size > len(data):
                raise ValueError("Invalid AVC NAL length")
            headers.append(data[offset])
            offset += size
    else:
        start = data.find(b"\x00\x00\x01")
        if start < 0 or any(data[:start]):
            raise ValueError("Invalid Annex B packet")
        while start >= 0:
            header = start + 3
            if header >= len(data):
                raise ValueError("Truncated Annex B NAL")
            headers.append(data[header])
            start = data.find(b"\x00\x00\x01", header + 1)
    if any(header & 0x80 or not 1 <= header & 0x1F <= 23 for header in headers):
        raise ValueError("Invalid elementary H264 NAL header")
    return tuple(header & 0x1F for header in headers)


def h264_is_idr(data: bytes, length_size: int | None = None) -> bool:
    """Require an actual IDR NAL, not an I-picture or an unverified keyframe flag."""
    return 5 in h264_nal_types(data, length_size)


@dataclass(frozen=True)
class H264Stream:
    """Native source geometry and codec configuration required to restart decoding."""

    width: int
    height: int
    extradata: bytes
    length_size: int | None = None
    time_base: Fraction = Fraction(1, 1)

    def __post_init__(self) -> None:
        if not 1 <= self.width <= 16384 or not 1 <= self.height <= 16384:
            raise ValueError("Invalid native stream geometry")
        if not isinstance(self.extradata, bytes) or len(self.extradata) > 65536:
            raise ValueError("Unbounded H264 codec configuration")
        if self.length_size is not None and self.length_size not in (1, 2, 3, 4):
            raise ValueError("Invalid AVC length-prefix size")
        if not isinstance(self.time_base, Fraction) or self.time_base <= 0:
            raise ValueError("Invalid encoded stream time base")


@dataclass(frozen=True)
class EncodedPacket:
    """One demuxed video packet, retained byte-for-byte in decoder order."""

    stream: H264Stream
    data: bytes
    dts: int | float
    pts: int | float
    duration: int = 0
    flags: int = 0

    @property
    def decode_time(self) -> float:
        """Return budget/ordering time without modifying the original packet ticks."""
        return float(self.dts * self.stream.time_base)


@dataclass(frozen=True)
class BufferUpdate:
    """Explicit availability/reset state for a future fail-open decoder controller."""

    stored: bool
    has_random_access: bool
    bytes_used: int
    reason: str | None = None


@dataclass(frozen=True)
class EncodedPreroll:
    """An IDR-aligned immutable packet window, never a decoded or resized video copy."""

    stream: H264Stream
    packets: tuple[EncodedPacket, ...]
    bytes_used: int
    lookback_seconds: float


class EncodedGopBuffer:
    """Evict complete GOPs and reject timestamp resets or changed codec configuration."""

    def __init__(self, max_bytes: int = 32 * 1024**2, max_seconds: float = 8) -> None:
        if (
            max_bytes < 1
            or not math.isfinite(max_seconds)
            or not 0 < max_seconds <= 120
        ):
            raise ValueError("Invalid encoded pre-roll limits")
        self.max_bytes = max_bytes
        self.max_seconds = max_seconds
        self.bytes_used = 0
        self.stream: H264Stream | None = None
        self.last_decode_time: float | None = None
        self.gops: deque[list[EncodedPacket]] = deque()

    def clear(self) -> None:
        """Invalidate all reference-chain state without changing source video or camera settings."""
        self.gops.clear()
        self.bytes_used = 0
        self.stream = None
        self.last_decode_time = None

    def append(self, packet: EncodedPacket) -> BufferUpdate:
        """Store a valid encoded packet or explicitly report why pre-roll is unavailable."""
        if (
            not math.isfinite(packet.dts)
            or not math.isfinite(packet.pts)
            or packet.dts == -(1 << 63)
            or packet.pts == -(1 << 63)
        ):
            self.clear()
            raise ValueError("Encoded packet timestamps must be finite")
        if not isinstance(packet.data, bytes) or not packet.data:
            self.clear()
            raise ValueError("Encoded packets must own immutable nonempty bytes")
        if len(packet.data) + len(packet.stream.extradata) > self.max_bytes:
            self.clear()
            return BufferUpdate(False, False, 0, "oversized_encoded_packet")
        try:
            idr = h264_is_idr(packet.data, packet.stream.length_size)
        except ValueError:
            self.clear()
            raise
        reason = None
        if self.stream is not None and self.stream != packet.stream:
            self.clear()
            reason = "stream_configuration_changed"
        decode_time = packet.decode_time
        if self.last_decode_time is not None and decode_time < self.last_decode_time:
            self.clear()
            reason = "decode_timestamp_reset"
        self.stream = packet.stream
        self.last_decode_time = decode_time
        if idr:
            self.gops.append([])
        elif not self.gops:
            return BufferUpdate(False, False, 0, reason or "awaiting_idr")
        if not self.bytes_used:
            self.bytes_used = len(packet.stream.extradata)
        self.gops[-1].append(packet)
        self.bytes_used += len(packet.data)
        while len(self.gops) > 1 and (
            self.bytes_used > self.max_bytes
            or decode_time - self.gops[0][0].decode_time > self.max_seconds
        ):
            oldest = self.gops.popleft()
            self.bytes_used -= sum(len(item.data) for item in oldest)
        if (
            self.bytes_used > self.max_bytes
            or decode_time - self.gops[0][0].decode_time > self.max_seconds
        ):
            self.clear()
            return BufferUpdate(False, False, 0, "idr_interval_exceeds_buffer_limit")
        return BufferUpdate(True, True, self.bytes_used, reason)

    def snapshot(self, minimum_seconds: float = 2) -> EncodedPreroll:
        """Require sufficient real packet history and begin at a verified IDR."""
        if (
            not math.isfinite(minimum_seconds)
            or not 0 <= minimum_seconds <= self.max_seconds
        ):
            raise ValueError("Invalid required pre-roll duration")
        if not self.gops or self.stream is None or self.last_decode_time is None:
            raise PrerollUnavailable("No complete random-access starting point")
        cutoff = self.last_decode_time - minimum_seconds
        eligible = [
            index for index, gop in enumerate(self.gops) if gop[0].decode_time <= cutoff
        ]
        if not eligible:
            raise PrerollUnavailable("Required compressed lookback is not buffered")
        packets = tuple(
            packet for gop in list(self.gops)[eligible[-1] :] for packet in gop
        )
        return EncodedPreroll(
            self.stream,
            packets,
            len(self.stream.extradata) + sum(len(item.data) for item in packets),
            self.last_decode_time - packets[0].decode_time,
        )
