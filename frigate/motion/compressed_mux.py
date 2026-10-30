"""Native H264 packet reframing/remuxing without an encoder or pixel decoder."""

from typing import IO

from frigate.motion.compressed_buffer import (
    EncodedPacket,
    H264Stream,
    h264_is_idr,
    h264_nal_types,
)

START_CODE = b"\x00\x00\x00\x01"


def avc_parameter_sets(extradata: bytes) -> tuple[tuple[bytes, ...], int]:
    """Read an AVC configuration record while preserving each coded parameter set."""
    if len(extradata) < 7 or extradata[0] != 1:
        raise ValueError("Invalid AVC configuration record")
    length_size = (extradata[4] & 3) + 1
    offset = 6
    parameters = []
    for group in range(2):
        if offset >= len(extradata):
            raise ValueError("Truncated AVC parameter-set record")
        count = extradata[5] & 31 if group == 0 else extradata[offset]
        if group:
            offset += 1
        if not count:
            raise ValueError("Missing AVC sequence/picture parameter sets")
        for _ in range(count):
            if offset + 2 > len(extradata):
                raise ValueError("Truncated AVC parameter-set length")
            size = int.from_bytes(extradata[offset : offset + 2], "big")
            offset += 2
            if not size or offset + size > len(extradata):
                raise ValueError("Invalid AVC parameter-set length")
            data = extradata[offset : offset + size]
            if data[0] & 0x1F != (7 if group == 0 else 8):
                raise ValueError("Unexpected AVC parameter-set NAL type")
            parameters.append(data)
            offset += size
    if offset < len(extradata):
        extension = extradata[offset:]
        if (
            len(extension) != 4
            or extradata[1] not in (100, 110, 122, 144)
            or extension[0] & 3 != 1
            or extension[1] & 7
            or extension[2] & 7
            or extension[3]
        ):
            raise ValueError("Unsupported AVC configuration extension")
    return tuple(parameters), length_size


def avc_packet_nals(data: bytes, length_size: int) -> tuple[bytes, ...]:
    """Split length-prefixed access units without entropy decoding or changing NAL payloads."""
    if length_size not in (1, 2, 3, 4) or not data:
        raise ValueError("Invalid AVC packet framing")
    offset = 0
    nals = []
    while offset < len(data):
        if offset + length_size > len(data):
            raise ValueError("Truncated AVC packet prefix")
        size = int.from_bytes(data[offset : offset + length_size], "big")
        offset += length_size
        if size < 1 or offset + size > len(data):
            raise ValueError("Invalid AVC packet NAL length")
        nals.append(data[offset : offset + size])
        offset += size
    return tuple(nals)


def annexb_packet(packet: EncodedPacket, *, include_headers: bool) -> bytes:
    """Reframe coded H264, retaining native slices rather than creating a resized video."""
    stream = packet.stream
    idr = h264_is_idr(packet.data, stream.length_size)
    if stream.length_size is None:
        headers = stream.extradata if include_headers and idr else b""
        if headers:
            h264_is_idr(headers)
        return headers + packet.data
    parameters, length_size = avc_parameter_sets(stream.extradata)
    if length_size != stream.length_size:
        raise ValueError("AVC packet/configuration framing differs")
    nals = avc_packet_nals(packet.data, length_size)
    if include_headers and idr:
        nals = parameters + nals
    return b"".join(START_CODE + nal for nal in nals)


class EncodedNutMuxer:
    """Supply a decoder with timestamped native H264; never instantiate a codec context."""

    def __init__(self, output: IO[bytes], stream: H264Stream) -> None:
        import av

        self.source = stream
        self.output = output
        self.container = av.open(
            output,
            mode="w",
            format="nut",
            options={"flush_packets": "1", "avoid_negative_ts": "disabled"},
        )
        self.stream = self.container.add_mux_stream(
            "h264",
            width=stream.width,
            height=stream.height,
            time_base=stream.time_base,
        )
        # Mux-only streams return None despite PyAV's non-optional context stub.
        codec_context: object | None = getattr(self.stream, "codec_context")
        if codec_context is not None:
            self.container.close()
            raise RuntimeError("Encoded-only mux unexpectedly created a codec context")
        self.started = False
        self.closed = False

    def write(self, packet: EncodedPacket) -> None:
        """Mux one immutable access unit; an independent decoder must begin at an IDR."""
        import av

        if self.closed:
            raise RuntimeError("Encoded transport is closed")
        if packet.stream != self.source:
            raise ValueError("Native mux stream configuration changed")
        if not isinstance(packet.pts, int) or not isinstance(packet.dts, int):
            raise ValueError("Native mux requires original integer packet timestamps")
        idr = h264_is_idr(packet.data, self.source.length_size)
        if not self.started and not idr:
            raise ValueError("Native decoder input must start with an IDR")
        data = annexb_packet(packet, include_headers=idr)
        if not self.started and not {7, 8} <= set(h264_nal_types(data)):
            raise ValueError(
                "Native decoder input lacks sequence/picture parameter sets"
            )
        encoded = av.Packet(data)
        encoded.pts = packet.pts
        encoded.dts = packet.dts
        encoded.duration = packet.duration
        encoded.time_base = self.source.time_base
        encoded.stream = self.stream
        encoded.is_keyframe = idr
        self.container.mux(encoded)
        self.output.flush()
        self.started = True

    def close(self) -> None:
        """Finish only the encoded transport container, without transforming any image plane."""
        if not self.closed:
            try:
                self.container.close()
                self.output.flush()
            finally:
                self.closed = True
