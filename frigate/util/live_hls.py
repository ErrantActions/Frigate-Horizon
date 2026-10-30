"""Plan private live HLS renditions without changing recording or camera sources."""

import hashlib
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, quote, urlsplit

if TYPE_CHECKING:
    from frigate.config import FrigateConfig

RESOURCE = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}\.(?:m3u8|mp4)$")
PACKAGER_ORIGIN = "http://127.0.0.1:8888"


@dataclass(frozen=True)
class LiveHlsRendition:
    """One advertised, main-derived H.264 rendition with a configured bitrate."""

    stream: str
    height: int
    bitrate: int

    @property
    def bandwidth(self) -> int:
        """Allow conservative video burst and audio headroom in the master."""
        return int(self.bitrate * 1000 * 1.35) + 256_000


def rendition_path(camera: str, height: int) -> str:
    """Keep arbitrary camera names out of private packager URL paths."""
    digest = hashlib.sha256(camera.encode()).hexdigest()[:24]
    return f"live_{digest}_{height}p"


def camera_renditions(
    config: "FrigateConfig", camera_name: str
) -> tuple[LiveHlsRendition, ...]:
    """Use only advertised main-derived profiles, including manually defined ones."""
    if camera_name == "birdseye":
        birdseye = config.birdseye
        if not birdseye.enabled or not birdseye.restream:
            return ()
        return (LiveHlsRendition("birdseye", birdseye.height, birdseye.bitrate),)
    camera = config.cameras.get(camera_name)
    if camera is None or not camera.enabled or not config.ui.live_compatibility:
        return ()
    advertised = set(camera.live.streams.values())
    generated = camera.live.transcode
    renditions = []
    for quality in sorted(generated.qualities, key=lambda value: value.bitrate):
        stream = f"{camera_name}_transcode_{quality.height}p"
        if quality.height < 720 or stream not in advertised:
            continue
        if not generated.enabled and stream not in config.go2rtc.streams:
            continue
        renditions.append(LiveHlsRendition(stream, quality.height, quality.bitrate))
    return tuple(renditions)


def packager_config(config: "FrigateConfig") -> dict:
    """Generate an on-demand packager bound only to the Frigate network loopback."""
    paths = {}
    for camera_name in (*config.cameras, "birdseye"):
        renditions = camera_renditions(config, camera_name)
        if not renditions:
            continue
        for rendition in renditions:
            paths[rendition_path(camera_name, rendition.height)] = {
                "source": f"rtsp://127.0.0.1:8554/{quote(rendition.stream, safe='')}",
                "sourceOnDemand": True,
                "sourceOnDemandStartTimeout": "20s",
                "sourceOnDemandCloseAfter": "10s",
                "rtspTransport": "tcp",
            }
    return {
        "logLevel": "warn",
        "api": False,
        "metrics": False,
        "pprof": False,
        "rtsp": False,
        "rtmp": False,
        "webrtc": False,
        "srt": False,
        "moq": False,
        "hls": True,
        "hlsAddress": "127.0.0.1:8888",
        "hlsAllowOrigins": [],
        "hlsAlwaysRemux": False,
        "hlsVariant": "lowLatency",
        "hlsSegmentCount": 7,
        "hlsSegmentDuration": "1s",
        "hlsPartDuration": "200ms",
        "hlsSegmentMaxSize": "8M",
        "hlsMuxerCloseAfter": "15s",
        "authInternalUsers": [
            {
                "user": "any",
                "ips": ["127.0.0.1", "::1"],
                "permissions": [{"action": "read"}],
            }
        ],
        "paths": paths,
    }


@dataclass(frozen=True)
class PackagerMaster:
    """Verified private bootstrap metadata; never expose its session value."""

    session: str = field(repr=False)
    video: str
    audio: str | None
    attributes: dict[str, str]


def _private_resource(
    uri: str, *, session_required: bool = True
) -> tuple[str, str | None]:
    parsed = urlsplit(uri)
    params = parse_qs(parsed.query, keep_blank_values=True)
    if (
        parsed.scheme
        or parsed.netloc
        or not RESOURCE.fullmatch(parsed.path)
        or ".." in parsed.path
        or (set(params) != {"session"} and (session_required or params))
        or (
            "session" in params
            and (
                len(params["session"]) != 1
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", params["session"][0])
            )
        )
    ):
        raise ValueError("Invalid private HLS bootstrap resource")
    return parsed.path, params["session"][0] if "session" in params else None


def parse_packager_master(text: str) -> PackagerMaster:
    """Read the pinned packager's single-video master and private session."""
    if not text.startswith("#EXTM3U"):
        raise ValueError("Invalid private HLS master")
    video = None
    audio = None
    session = None
    attributes = {}
    for line in text.splitlines():
        if line.startswith("#EXT-X-STREAM-INF:"):
            attributes = dict(re.findall(r'([A-Z0-9-]+)=("[^"\r\n]*"|[^,\r\n]*)', line))
        elif line.startswith("#EXT-X-MEDIA:TYPE=AUDIO,"):
            match = re.search(r'URI="([^"]+)"', line)
            if match:
                audio, audio_session = _private_resource(match.group(1))
                if session is not None and session != audio_session:
                    raise ValueError("Private HLS session mismatch")
                session = audio_session
        elif line and not line.startswith("#"):
            if video is not None:
                raise ValueError("Unexpected multiple private video variants")
            video, video_session = _private_resource(line)
            if session is not None and session != video_session:
                raise ValueError("Private HLS session mismatch")
            session = video_session
    if video is None or session is None or not attributes:
        raise ValueError("Private HLS master has no video variant")
    patterns = {
        "CODECS": r'"[A-Za-z0-9.,_-]{1,128}"',
        "RESOLUTION": r"[0-9]{1,5}x[0-9]{1,5}",
        "FRAME-RATE": r"[0-9]{1,3}(?:\.[0-9]{1,6})?",
    }
    if any(
        name in attributes and not re.fullmatch(pattern, attributes[name])
        for name, pattern in patterns.items()
    ):
        raise ValueError("Invalid private HLS video attributes")
    return PackagerMaster(session, video, audio, attributes)


def master_playlist(
    variants: tuple[tuple[LiveHlsRendition, PackagerMaster], ...],
    *,
    audio: bool = True,
) -> str:
    """Combine measured codec metadata while keeping packager sessions private."""
    lines = ["#EXTM3U", "#EXT-X-VERSION:10"]
    for rendition, master in variants:
        if audio and master.audio:
            lines.append(
                f'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio{rendition.height}",'
                f'NAME="Audio",AUTOSELECT=YES,DEFAULT=YES,'
                f'URI="{rendition.height}p/{master.audio}"'
            )
    for rendition, master in variants:
        attributes = [
            f"BANDWIDTH={rendition.bandwidth}",
            f"AVERAGE-BANDWIDTH={rendition.bitrate * 1000}",
        ]
        for name in ("CODECS", "RESOLUTION", "FRAME-RATE"):
            if name not in master.attributes:
                continue
            value = master.attributes[name]
            if name == "CODECS" and not audio:
                codecs = [
                    codec
                    for codec in value.strip('"').split(",")
                    if codec.startswith(
                        ("avc1.", "avc3.", "hvc1.", "hev1.", "av01.", "vp09.")
                    )
                ]
                if codecs:
                    value = '"' + ",".join(codecs) + '"'
                else:
                    continue
            attributes.append(f"{name}={value}")
        if audio and master.audio:
            attributes.append(f'AUDIO="audio{rendition.height}"')
        lines.extend(
            [
                "#EXT-X-STREAM-INF:" + ",".join(attributes),
                f"{rendition.height}p/{master.video}",
            ]
        )
    return "\n".join(lines) + "\n"


def public_media_playlist(text: str) -> str:
    """Remove private packager sessions from every media-playlist resource URI."""
    if not text.startswith("#EXTM3U"):
        raise ValueError("Invalid private HLS media playlist")

    def public_uri(uri: str) -> str:
        resource, _session = _private_resource(uri, session_required=False)
        return resource

    lines = []
    for line in text.splitlines():
        if line and not line.startswith("#"):
            line = public_uri(line)
        else:
            line = re.sub(
                r'URI="([^"]+)"',
                lambda match: f'URI="{public_uri(match.group(1))}"',
                line,
            )
        lines.append(line)
    return "\n".join(lines) + "\n"


def resource_url(camera: str, height: int, resource: str) -> str:
    """Accept only flat packager resources, never arbitrary upstream paths or URLs."""
    if not RESOURCE.fullmatch(resource) or ".." in resource:
        raise ValueError("Invalid live HLS resource")
    return f"{PACKAGER_ORIGIN}/{rendition_path(camera, height)}/{resource}"
