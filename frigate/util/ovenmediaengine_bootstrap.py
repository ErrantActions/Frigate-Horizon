"""Generate private, viewer-only OME and authenticated Coturn configuration."""

import hashlib
import json
import logging
import os
import secrets
import ssl
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from pydantic import ValidationError
from ruamel.yaml import YAML, YAMLError

from frigate.comms.live_webrtc import (
    DETAIL_BITRATES,
    DETAIL_HEIGHTS,
    RUNTIME,
    application_name,
)
from frigate.config.auth import AuthConfig
from frigate.config.live_webrtc import LiveWebRTCConfig
from frigate.util.config import find_config_file

logger = logging.getLogger(__name__)


def element(parent: ET.Element, name: str, value: str | int) -> ET.Element:
    """Append XML text without interpolating configuration into markup."""
    child = ET.SubElement(parent, name)
    child.text = str(value)
    return child


def server_xml(
    settings: LiveWebRTCConfig, api_token: str, runtime: Path = RUNTIME
) -> str:
    """Configure bounded CPU profiles without any camera inputs or origins."""
    root = ET.Element("Server", version="8")
    for key, value in (
        ("Name", "FrigateLive"),
        ("Type", "origin"),
        ("IP", "127.0.0.1"),
        ("PrivacyProtection", "true"),
    ):
        element(root, key, value)
    modules = ET.SubElement(root, "Modules")
    for module in ("HTTP2", "LLHLS", "P2P"):
        element(ET.SubElement(modules, module), "Enable", "false")
    pool = ET.SubElement(modules, "TaskPool")
    element(pool, "ThreadCount", 2)
    element(pool, "MaxTasks", 128)
    bind = ET.SubElement(root, "Bind")
    api = ET.SubElement(ET.SubElement(bind, "Managers"), "API")
    element(api, "Port", 8081)
    element(api, "WorkerCount", 1)
    pull = ET.SubElement(ET.SubElement(bind, "Providers"), "RTSPC")
    element(pull, "WorkerCount", 1)
    publisher = ET.SubElement(ET.SubElement(bind, "Publishers"), "WebRTC")
    signaling = ET.SubElement(publisher, "Signalling")
    element(signaling, "Port", 3333)
    element(signaling, "WorkerCount", 1)
    ice = ET.SubElement(publisher, "IceCandidates")
    element(ice, "BindIP", "0.0.0.0")
    for host in settings.advertised_hosts:
        element(ice, "IceCandidate", f"{host}:{settings.udp_port}/udp")
    element(ice, "DefaultTransport", "udp")
    element(ice, "IceWorkerCount", 1)
    element(
        ET.SubElement(ET.SubElement(root, "Managers"), "API"), "AccessToken", api_token
    )
    vhost = ET.SubElement(ET.SubElement(root, "VirtualHosts"), "VirtualHost")
    element(vhost, "Name", "default")
    element(ET.SubElement(ET.SubElement(vhost, "Host"), "Names"), "Name", "*")
    applications = ET.SubElement(vhost, "Applications")
    caps = [height for height in DETAIL_HEIGHTS if height <= settings.max_height]
    for view, maximum in [
        ("grid", settings.grid_height),
        *[("detail", h) for h in caps],
    ]:
        heights = (
            [settings.grid_height]
            if view == "grid"
            else [height for height in DETAIL_HEIGHTS if height <= maximum]
        )
        fps = settings.grid_fps if view == "grid" else settings.detail_fps
        for audio in (False, True):
            app = ET.SubElement(applications, "Application")
            element(app, "Name", application_name(view, maximum, audio))
            element(app, "Type", "live")
            profiles = ET.SubElement(app, "OutputProfiles")
            element(
                ET.SubElement(profiles, "Decodes"),
                "ThreadCount",
                settings.encoder_threads,
            )
            profile = ET.SubElement(profiles, "OutputProfile")
            element(profile, "Name", "viewer_cpu")
            element(profile, "OutputStreamName", "${OriginStreamName}")
            playlist = ET.SubElement(profile, "Playlist")
            element(playlist, "Name", "live")
            element(playlist, "FileName", "master")
            element(ET.SubElement(playlist, "Options"), "WebRtcAutoAbr", "true")
            encodes = ET.SubElement(profile, "Encodes")
            for height in heights:
                rendition = ET.SubElement(playlist, "Rendition")
                element(rendition, "Name", f"{height}p")
                element(rendition, "Video", f"v{height}")
                if audio:
                    element(rendition, "Audio", "opus")
                video = ET.SubElement(encodes, "Video")
                for key, value in (
                    ("Name", f"v{height}"),
                    ("Codec", "h264"),
                    ("Height", height),
                    ("Framerate", fps),
                    (
                        "Bitrate",
                        (500 if view == "grid" else DETAIL_BITRATES[height]) * 1000,
                    ),
                    ("ThreadCount", settings.encoder_threads),
                    ("KeyFrameIntervalType", "frame"),
                    ("KeyFrameInterval", fps),
                    ("Profile", "baseline"),
                    ("BFrames", 0),
                    ("Lookahead", 0),
                ):
                    element(video, key, value)
            if audio:
                track = ET.SubElement(encodes, "Audio")
                for key, value in (
                    ("Name", "opus"),
                    ("Codec", "opus"),
                    ("Bitrate", 64000),
                    ("Samplerate", 48000),
                    ("Channel", 2),
                ):
                    element(track, key, value)
            providers = ET.SubElement(app, "Providers")
            ET.SubElement(providers, "RTSPPull")
            if view == "detail":
                name = application_name(view, maximum, audio)
                schedule = ET.SubElement(providers, "Schedule")
                element(
                    schedule, "MediaRootDir", str(runtime / "recorded-media" / name)
                )
                # OME watches every file in this directory, regardless of app identity.
                element(schedule, "ScheduleFilesDir", str(runtime / "schedules" / name))
            rtc = ET.SubElement(ET.SubElement(app, "Publishers"), "WebRTC")
            element(rtc, "Timeout", 10000)
    ET.indent(root)
    return ET.tostring(root, encoding="unicode", xml_declaration=True)


def turn_config(settings: LiveWebRTCConfig, secret: str) -> str:
    """Restrict temporary authenticated relay allocations to the media endpoint."""
    relay = settings.turn
    lines = [
        f"listening-ip={relay.listen_ip}",
        f"relay-ip={relay.relay_ip}",
        f"listening-port={relay.port}",
        f"min-port={relay.min_port}",
        f"max-port={relay.max_port}",
        f"realm={relay.hostname}",
        "use-auth-secret",
        f"static-auth-secret={secret}",
        "fingerprint",
        "no-cli",
        "no-rfc5780",
        "no-multicast-peers",
        "no-ipv6-relay",
        "no-dtls",
        "relay-threads=2",
        "user-quota=4",
        f"total-quota={relay.max_allocations}",
        "max-bps=2500000",
        "bps-capacity=200000000",
        "max-allocate-lifetime=600",
        "denied-peer-ip=0.0.0.0-255.255.255.255",
        *[f"allowed-peer-ip={host}" for host in settings.advertised_hosts],
    ]
    if relay.external_ip:
        lines.append(f"external-ip={relay.external_ip}/{relay.relay_ip}")
    if relay.tls:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(relay.certificate, relay.private_key)
        lines.extend(
            [
                f"tls-listening-port={relay.tls_port}",
                f"cert={relay.certificate}",
                f"pkey={relay.private_key}",
                "no-tlsv1",
                "no-tlsv1_1",
            ]
        )
    else:
        lines.append("no-tls")
    return "\n".join(lines) + "\n"


def write_private(path: Path, content: str) -> None:
    """Replace runtime material atomically without a world-readable intermediate."""
    descriptor, name = tempfile.mkstemp(prefix=".live-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def generate_runtime(settings: LiveWebRTCConfig, runtime: Path = RUNTIME) -> None:
    """Publish enable markers last, after all private configuration is ready."""
    if runtime.is_symlink():
        raise ValueError("Private media state must not be a symlink")
    runtime.mkdir(mode=0o700, exist_ok=True)
    runtime.chmod(0o700)
    for marker in ("enabled", "turn-enabled"):
        (runtime / marker).unlink(missing_ok=True)
    if not settings.enabled:
        return
    api_token, turn_secret = secrets.token_urlsafe(48), secrets.token_urlsafe(48)
    relay = turn_config(settings, turn_secret) if settings.turn.enabled else None
    metadata = {
        "settings_sha256": hashlib.sha256(
            settings.model_dump_json().encode()
        ).hexdigest(),
        "api_token": api_token,
        "turn_secret": turn_secret,
        "recorded_webrtc_version": 1,
    }
    for kind in ("recorded-media", "schedules"):
        root = runtime / kind
        if root.is_symlink():
            raise ValueError("Private media state must not be a symlink")
        root.mkdir(mode=0o700, exist_ok=True)
        root.chmod(0o700)
        for height in DETAIL_HEIGHTS:
            if height > settings.max_height:
                continue
            for audio in (False, True):
                directory = root / application_name("detail", height, audio)
                if directory.is_symlink():
                    raise ValueError("Private media state must not be a symlink")
                directory.mkdir(mode=0o700, exist_ok=True)
                directory.chmod(0o700)
    write_private(runtime / "Server.xml", server_xml(settings, api_token, runtime))
    write_private(
        runtime / "Logger.xml",
        '<Logger version="2"><File enabled="false"/><Tag name=".*" level="warn"/></Logger>',
    )
    write_private(runtime / "runtime.json", json.dumps(metadata))
    if relay is not None:
        write_private(runtime / "turnserver.conf", relay)
        write_private(runtime / "turn-enabled", "")
    write_private(runtime / "enabled", "")


def main() -> int:
    """Load only live-media settings, leaving recorder initialization independent."""
    try:
        path = Path(find_config_file())
        with path.open(encoding="utf-8") as source:
            raw = (
                json.load(source)
                if path.suffix == ".json"
                else YAML(typ="safe").load(source)
            )
        if not isinstance(raw, dict):
            raise ValueError("Expected configuration mapping")
        settings = LiveWebRTCConfig.model_validate(raw.get("live_webrtc", {}))
        if (
            settings.enabled
            and not AuthConfig.model_validate(raw.get("auth", {})).enabled
        ):
            raise ValueError("WebRTC requires native authentication")
        generate_runtime(settings)
    except (OSError, ValueError, TypeError, ValidationError, YAMLError) as error:
        logger.error(
            "Live-media configuration failed (%s); viewing remains disabled",
            type(error).__name__,
        )
        return 1
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
