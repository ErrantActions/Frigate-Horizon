"""Pinned, authenticated RTSP replay transport for a single local FFmpeg consumer."""

import asyncio
import base64
import contextlib
import hashlib
import ipaddress
import re
import secrets
import socket
import ssl
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import unquote, urlsplit, urlunsplit
from urllib.request import parse_http_list, parse_keqv_list

MAX_MESSAGE = 1_048_576
RPC_TIMEOUT = 10


class ReplayUnsupported(ValueError):
    """The device cannot safely fulfill a bounded ONVIF replay request."""


@dataclass(frozen=True)
class ReplaySource:
    """Private source descriptor, never serialized into an API response."""

    uri: str = field(repr=False)
    username: str = field(repr=False)
    password: str = field(repr=False)
    address: str
    port: int
    hostname: str
    tls: bool = False
    tls_insecure: bool = False
    recorded_audio: bool | None = None


async def pin_replay_source(uri, connection):
    """Allow only the saved camera's host and pin its resolved address once."""
    try:
        parsed = urlsplit(uri)
        saved_host = connection.host
        if "://" not in saved_host:
            with contextlib.suppress(ValueError):
                if ipaddress.ip_address(saved_host).version == 6:
                    saved_host = f"[{saved_host}]"
            saved_host = f"http://{saved_host}"
        saved = urlsplit(saved_host)
        if (
            parsed.scheme not in ("rtsp", "rtsps")
            or not parsed.hostname
            or not saved.hostname
        ):
            raise ReplayUnsupported("Unsupported replay transport")
        if len(uri) > 8192 or any(ord(character) <= 32 for character in uri):
            raise ReplayUnsupported("Invalid replay address")
        port = (
            parsed.port
            if parsed.port is not None
            else (322 if parsed.scheme == "rtsps" else 554)
        )
        if not 1 <= port <= 65535:
            raise ReplayUnsupported("Invalid replay port")
        resolved = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(
                saved.hostname, port, type=socket.SOCK_STREAM
            ),
            RPC_TIMEOUT,
        )
        addresses = list(dict.fromkeys(item[4][0] for item in resolved))
        addresses = [
            address
            for address in addresses
            if not (
                ipaddress.ip_address(address).is_loopback
                or ipaddress.ip_address(address).is_link_local
                or ipaddress.ip_address(address).is_multicast
                or ipaddress.ip_address(address).is_unspecified
            )
        ]
        if not addresses:
            raise ReplayUnsupported("No eligible camera address")
        if parsed.hostname.lower() != saved.hostname.lower():
            try:
                advertised_ip = str(ipaddress.ip_address(parsed.hostname))
            except ValueError:
                raise ReplayUnsupported(
                    "Replay address does not belong to this camera"
                ) from None
            if advertised_ip not in addresses:
                raise ReplayUnsupported("Replay address does not belong to this camera")
            address = advertised_ip
        else:
            address = addresses[0]
        host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        authority = host + (f":{parsed.port}" if parsed.port else "")
        clean_uri = urlunsplit(
            (parsed.scheme, authority, parsed.path or "/", parsed.query, "")
        )
        username = (
            unquote(parsed.username)
            if parsed.username is not None
            else connection.username
        )
        password = (
            unquote(parsed.password)
            if parsed.password is not None
            else connection.password.get_secret_value()
        )
        if any(ord(character) < 32 for character in username + password):
            raise ReplayUnsupported("Invalid camera replay authentication")
        return ReplaySource(
            clean_uri,
            username,
            password,
            address,
            port,
            parsed.hostname,
            parsed.scheme == "rtsps",
            connection.tls_insecure,
        )
    except (OSError, ValueError, TypeError):
        raise ReplayUnsupported(
            "Camera replay address is unavailable or disallowed"
        ) from None


def clock_range(start: datetime, end: datetime) -> str:
    """Encode the fixed UTC interval required by ONVIF replay PLAY."""

    def timestamp(value):
        return value.astimezone(UTC).strftime("%Y%m%dT%H%M%S.%fZ")

    return f"clock={timestamp(start)}-{timestamp(end)}"


async def read_message(reader, first=b""):
    """Read bounded RTSP headers and body; never parse arbitrary HTTP/UDP traffic."""
    try:
        header = first + await reader.readuntil(b"\r\n\r\n")
        if len(header) > 65536:
            raise ReplayUnsupported("RTSP headers exceed the supported size")
        lines = header.decode("latin-1").split("\r\n")
        headers = {}
        for line in lines[1:]:
            if line:
                key, value = line.split(":", 1)
                key = key.strip().lower()
                if any(ord(character) < 32 for character in key + value):
                    raise ReplayUnsupported("Invalid RTSP header")
                if key in headers and key == "www-authenticate":
                    if value.strip().lower().startswith("digest "):
                        headers[key] = value.strip()
                    continue
                if key in headers:
                    raise ReplayUnsupported("Duplicate RTSP header")
                headers[key] = value.strip()
        length = int(headers.get("content-length", "0"))
        if not 0 <= length <= MAX_MESSAGE:
            raise ReplayUnsupported("RTSP body exceeds the supported size")
        return lines[0], headers, await reader.readexactly(length)
    except (ValueError, UnicodeError, asyncio.LimitOverrunError):
        raise ReplayUnsupported("Invalid RTSP response") from None


class ReplayGateway:
    """Force a fixed clock range while keeping camera authentication off argv."""

    def __init__(self, source: ReplaySource, start: datetime, end: datetime):
        self.source = source
        self.start = start
        self.end = end
        self.nonce = secrets.token_hex(24)
        self.server = None
        self.local_uri = None
        self.downstream = None
        self.upstream = None
        self.upstream_reader = None
        self.handler = None
        self.pending = {}
        self.sequence = 0
        self.challenge = None
        self.auth_count = 0
        self.session = None
        self.targets = {}
        self.frames = []
        self.buffered_bytes = 0
        self.playing = False
        self.closed = False
        self.error_code = None
        self._accepted = False

    async def start_server(self):
        """Open an unpredictable, one-consumer loopback endpoint only."""
        self.server = await asyncio.start_server(
            self._accept,
            "127.0.0.1",
            0,
            limit=65536,
        )
        port = self.server.sockets[0].getsockname()[1]
        self.local_uri = f"rtsp://127.0.0.1:{port}/{self.nonce}"
        self.targets[self.local_uri] = self.source.uri
        self.targets[self.local_uri + "/"] = self.source.uri
        return self.local_uri

    def _authorization(self, method, uri):
        if self.challenge is None:
            return None
        scheme, value = self.challenge.split(" ", 1)
        if scheme.lower() == "basic":
            encoded = base64.b64encode(
                f"{self.source.username}:{self.source.password}".encode()
            ).decode()
            return f"Basic {encoded}"
        if scheme.lower() != "digest":
            raise ReplayUnsupported("Unsupported camera replay authentication")
        parameters = parse_keqv_list(parse_http_list(value))
        realm = parameters.get("realm")
        nonce = parameters.get("nonce")
        algorithm = parameters.get("algorithm", "MD5")
        name = algorithm.lower().removesuffix("-sess").replace("-", "")
        if (
            not realm
            or not nonce
            or algorithm.lower() not in ("md5", "md5-sess", "sha-256", "sha-256-sess")
        ):
            raise ReplayUnsupported("Unsupported camera replay authentication")
        qops = parameters.get("qop", "").split(",")
        if parameters.get("qop") and "auth" not in [item.strip() for item in qops]:
            raise ReplayUnsupported("Unsupported camera replay authentication")
        cnonce = secrets.token_hex(16)
        self.auth_count += 1
        count = f"{self.auth_count:08x}"

        def digest(text):
            return hashlib.new(name, text.encode(), usedforsecurity=False).hexdigest()

        first = digest(f"{self.source.username}:{realm}:{self.source.password}")
        if algorithm.lower().endswith("-sess"):
            first = digest(f"{first}:{nonce}:{cnonce}")
        second = digest(f"{method}:{uri}")
        response = (
            digest(f"{first}:{nonce}:{count}:{cnonce}:auth:{second}")
            if parameters.get("qop")
            else digest(f"{first}:{nonce}:{second}")
        )

        def quote(text):
            return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'

        fields = {
            "username": self.source.username,
            "realm": realm,
            "nonce": nonce,
            "uri": uri,
            "response": response,
        }
        if "opaque" in parameters:
            fields["opaque"] = parameters["opaque"]
        result = ", ".join(f"{key}={quote(text)}" for key, text in fields.items())
        result += f", algorithm={algorithm}"
        if parameters.get("qop"):
            result += f", qop=auth, nc={count}, cnonce={quote(cnonce)}"
        elif algorithm.lower().endswith("-sess"):
            result += f", cnonce={quote(cnonce)}"
        return f"Digest {result}"

    async def _rpc(self, method, target, headers, body=b""):
        for _ in range(2):
            self.sequence += 1
            sequence = str(self.sequence)
            outgoing = {
                **headers,
                "CSeq": sequence,
                "User-Agent": "Horizon-ONVIF-Replay",
            }
            authorization = self._authorization(method, target)
            if authorization:
                outgoing["Authorization"] = authorization
            if self.session:
                outgoing["Session"] = self.session
            if body:
                outgoing["Content-Length"] = str(len(body))
            request = f"{method} {target} RTSP/1.0\r\n"
            request += "".join(f"{key}: {value}\r\n" for key, value in outgoing.items())
            future = asyncio.get_running_loop().create_future()
            self.pending[sequence] = future
            try:
                self.upstream.write(request.encode("latin-1") + b"\r\n" + body)
                await self.upstream.drain()
                status, response_headers, response_body = await asyncio.wait_for(
                    future, RPC_TIMEOUT
                )
            finally:
                self.pending.pop(sequence, None)
            if status != 401:
                return status, response_headers, response_body
            challenge = response_headers.get("www-authenticate")
            if not challenge:
                break
            if challenge != self.challenge:
                self.auth_count = 0
            self.challenge = challenge
        raise ReplayUnsupported("Camera rejected replay authentication")

    async def _read_upstream(self, reader):
        try:
            while not self.closed:
                first = await reader.readexactly(1)
                if first == b"$":
                    header = await reader.readexactly(3)
                    packet = (
                        first
                        + header
                        + await reader.readexactly(int.from_bytes(header[1:], "big"))
                    )
                    if self.playing:
                        self.downstream.write(packet)
                        await self.downstream.drain()
                    else:
                        self.buffered_bytes += len(packet)
                        if self.buffered_bytes > MAX_MESSAGE:
                            raise ReplayUnsupported("Excess replay preroll")
                        self.frames.append(packet)
                    continue
                line, headers, body = await read_message(reader, first)
                protocol, code, *_reason = line.split(" ")
                if protocol != "RTSP/1.0":
                    raise ReplayUnsupported("Invalid replay response protocol")
                future = self.pending.get(headers.get("cseq"))
                if future is None:
                    raise ReplayUnsupported("Unmatched replay response")
                if not future.done():
                    future.set_result((int(code), headers, body))
        except (OSError, ValueError, asyncio.IncompleteReadError) as error:
            if not self.closed:
                self.error_code = (
                    "camera_ended"
                    if self.playing and isinstance(error, asyncio.IncompleteReadError)
                    else "camera_unavailable"
                )
            if self.downstream:
                self.downstream.close()
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(
                        ReplayUnsupported("Camera replay connection ended")
                    )

    def _target(self, uri, base):
        if len(uri) > 8192 or any(ord(character) <= 32 for character in uri):
            raise ReplayUnsupported("Invalid replay track address")
        value = urlsplit(uri)
        original = urlsplit(self.source.uri)
        if value.scheme:
            if (
                value.scheme != original.scheme
                or value.hostname != original.hostname
                or (value.port or self.source.port) != self.source.port
            ):
                raise ReplayUnsupported("Replay track leaves the pinned camera")
            host = f"[{value.hostname}]" if ":" in value.hostname else value.hostname
            authority = host + (f":{value.port}" if value.port else "")
            return urlunsplit((value.scheme, authority, value.path, value.query, ""))
        root = urlsplit(base)
        path = (
            value.path
            if value.path.startswith("/")
            else root.path.rstrip("/") + "/" + value.path
        )
        return urlunsplit((root.scheme, root.netloc, path, value.query, ""))

    def _sdp(self, body, base):
        result = []
        for line in body.decode("utf-8").splitlines():
            if line.startswith("a=control:") and line != "a=control:*":
                target = self._target(line[len("a=control:") :], base)
                local = f"{self.local_uri}/track/{len(self.targets)}"
                self.targets[local] = target
                line = f"a=control:{local}"
            result.append(line)
        return ("\r\n".join(result) + "\r\n").encode()

    def _validate_range(self, value):
        if not value or not value.startswith("clock="):
            raise ReplayUnsupported("Camera did not acknowledge an ONVIF clock range")
        try:
            actual_start = datetime.fromisoformat(value[6:].split("-", 1)[0])
            if (
                actual_start.tzinfo is None
                or abs((actual_start - self.start).total_seconds()) > 5
            ):
                raise ValueError
        except (ValueError, TypeError):
            raise ReplayUnsupported(
                "Camera did not acknowledge the requested recording time"
            ) from None

    async def _accept(self, reader, writer):
        if self.closed or self._accepted:
            writer.close()
            await writer.wait_closed()
            return
        self._accepted = True
        self.handler = asyncio.current_task()
        self.downstream = writer
        try:
            first = await asyncio.wait_for(reader.readexactly(1), RPC_TIMEOUT)
            initial = await asyncio.wait_for(read_message(reader, first), RPC_TIMEOUT)
            initial_parts = initial[0].split(" ")
            if len(initial_parts) != 3 or initial_parts[1] not in self.targets:
                raise ReplayUnsupported("Unknown local replay consumer")
            tls = None
            if self.source.tls:
                tls = await asyncio.to_thread(ssl.create_default_context)
                if self.source.tls_insecure:
                    tls.check_hostname = False
                    tls.verify_mode = ssl.CERT_NONE
            upstream_reader, self.upstream = await asyncio.wait_for(
                asyncio.open_connection(
                    self.source.address,
                    self.source.port,
                    ssl=tls,
                    server_hostname=self.source.hostname if tls else None,
                    limit=65536,
                ),
                RPC_TIMEOUT,
            )
            self.upstream_reader = asyncio.create_task(
                self._read_upstream(upstream_reader)
            )
            while not self.closed:
                if initial is not None:
                    line, headers, body = initial
                    initial = None
                else:
                    first = await reader.readexactly(1)
                    if first == b"$":
                        header = await reader.readexactly(3)
                        packet = await reader.readexactly(
                            int.from_bytes(header[1:], "big")
                        )
                        self.upstream.write(first + header + packet)
                        await self.upstream.drain()
                        continue
                    line, headers, body = await read_message(reader, first)
                method, uri, protocol = line.split(" ")
                if (
                    protocol != "RTSP/1.0"
                    or method
                    not in {
                        "OPTIONS",
                        "DESCRIBE",
                        "SETUP",
                        "PLAY",
                        "PAUSE",
                        "GET_PARAMETER",
                        "TEARDOWN",
                    }
                    or (uri != "*" and uri not in self.targets)
                    or body
                ):
                    raise ReplayUnsupported("Unsupported local replay operation")
                target = self.targets.get(uri, self.source.uri)
                outgoing = {}
                if method == "DESCRIBE":
                    outgoing["Accept"] = "application/sdp"
                if method == "SETUP":
                    channels = re.search(
                        r"interleaved=(\d{1,3})-(\d{1,3})", headers.get("transport", "")
                    )
                    if not channels or any(
                        int(channel) > 255 for channel in channels.groups()
                    ):
                        raise ReplayUnsupported(
                            "Replay requires interleaved TCP transport"
                        )
                    outgoing["Transport"] = (
                        f"RTP/AVP/TCP;unicast;interleaved={channels.group(1)}-{channels.group(2)}"
                    )
                if method == "PLAY":
                    outgoing.update(
                        {
                            "Require": "onvif-replay",
                            "Range": clock_range(self.start, self.end),
                            "Rate-Control": "no",
                            "Scale": "1.0",
                        }
                    )
                status, response_headers, response_body = await self._rpc(
                    method, target, outgoing
                )
                if status != 200:
                    self.error_code = (
                        "unsupported_camera"
                        if status in (405, 451, 454, 455, 457, 461, 551)
                        else "camera_unavailable"
                    )
                    raise ReplayUnsupported("Camera rejected bounded replay")
                if "session" in response_headers:
                    self.session = response_headers["session"].split(";", 1)[0]
                downstream_headers = {"CSeq": headers.get("cseq", "0")}
                if method == "OPTIONS":
                    downstream_headers["Public"] = (
                        "OPTIONS, DESCRIBE, SETUP, PLAY, TEARDOWN"
                    )
                if method == "DESCRIBE":
                    base = response_headers.get("content-base", self.source.uri)
                    self._target(base, self.source.uri)
                    response_body = self._sdp(response_body, base)
                    downstream_headers.update(
                        {
                            "Content-Type": "application/sdp",
                            "Content-Base": self.local_uri + "/",
                        }
                    )
                if method == "SETUP":
                    transport = response_headers.get("transport", "")
                    if not transport.upper().startswith("RTP/AVP/TCP"):
                        raise ReplayUnsupported("Camera rejected TCP replay")
                    downstream_headers["Transport"] = transport
                if self.session:
                    downstream_headers["Session"] = self.nonce
                if method == "PLAY":
                    self._validate_range(response_headers.get("range"))
                    downstream_headers["Range"] = response_headers["range"]
                downstream_headers["Content-Length"] = str(len(response_body))
                response = "RTSP/1.0 200 OK\r\n" + "".join(
                    f"{key}: {value}\r\n" for key, value in downstream_headers.items()
                )
                writer.write(response.encode("latin-1") + b"\r\n" + response_body)
                await writer.drain()
                if method == "PLAY":
                    self.playing = True
                    for packet in self.frames:
                        writer.write(packet)
                    self.frames.clear()
                    self.buffered_bytes = 0
                    await writer.drain()
                if method == "TEARDOWN":
                    break
        except (
            OSError,
            ValueError,
            UnicodeError,
            asyncio.IncompleteReadError,
            TimeoutError,
        ):
            self.error_code = self.error_code or "unsupported_camera"
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
            if self.upstream:
                self.upstream.close()

    async def close(self):
        """Close the bounded camera session and all loopback/RTP tasks."""
        if self.closed:
            return
        self.closed = True
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        if self.upstream:
            if self.session:
                with contextlib.suppress(OSError, ValueError, TimeoutError):
                    self.sequence += 1
                    request = (
                        f"TEARDOWN {self.source.uri} RTSP/1.0\r\n"
                        f"CSeq: {self.sequence}\r\nSession: {self.session}\r\n"
                    )
                    authorization = self._authorization("TEARDOWN", self.source.uri)
                    if authorization:
                        request += f"Authorization: {authorization}\r\n"
                    request += "\r\n"
                    self.upstream.write(request.encode("latin-1"))
                    await asyncio.wait_for(self.upstream.drain(), 1)
            self.upstream.close()
        if self.downstream:
            self.downstream.close()
        tasks = [
            task
            for task in (self.handler, self.upstream_reader)
            if task and task is not asyncio.current_task()
        ]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for writer in (self.upstream, self.downstream):
            if writer:
                with contextlib.suppress(OSError, TimeoutError):
                    await asyncio.wait_for(writer.wait_closed(), 1)
