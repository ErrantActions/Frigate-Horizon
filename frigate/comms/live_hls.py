"""Lifecycle and bounded access to the private live HLS packager."""

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from frigate.plugins.packages import write_private_json
from frigate.util.live_hls import (
    LiveHlsRendition,
    PackagerMaster,
    master_playlist,
    packager_config,
    parse_packager_master,
    public_media_playlist,
    resource_url,
)

if TYPE_CHECKING:
    from frigate.config import FrigateConfig

logger = logging.getLogger(__name__)
MAX_RESOURCE_BYTES = 8 * 1024 * 1024


class _PrivateHlsHttpLog(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if "127.0.0.1:8888" in record.getMessage():
            record.msg = "Private live HLS request"
            record.args = ()
        return True


logging.getLogger("httpx").addFilter(_PrivateHlsHttpLog())


class LiveHlsError(RuntimeError):
    """A public error code, not an upstream body, URL or credential."""

    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


class LiveHlsManager:
    """Share one connection pool and atomically update private packager paths."""

    def __init__(
        self,
        config: Callable[[], "FrigateConfig"],
        path: Path,
    ) -> None:
        self.config = config
        self.path = path
        self.client: httpx.AsyncClient | None = None
        self.lock = asyncio.Lock()
        self.last_config: dict | None = None
        self.startup_error: LiveHlsError | None = None
        self.sessions: dict[tuple[str, int], PackagerMaster] = {}
        self.session_locks: dict[tuple[str, int], asyncio.Lock] = {}

    async def start(self) -> None:
        """Create the bounded local client without starting camera producers."""
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(30, connect=2, pool=2),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
            follow_redirects=False,
            trust_env=False,
        )
        if self.config().ui.live_hls:
            try:
                await self.sync_config()
            except LiveHlsError as error:
                self.startup_error = error

    async def sync_config(self) -> None:
        """Publish only the currently configured and enabled camera renditions."""
        async with self.lock:
            rendered = packager_config(self.config())
            if rendered == self.last_config:
                return
            try:
                await asyncio.to_thread(write_private_json, self.path, rendered)
            except OSError:
                logger.exception("Could not write private live HLS configuration")
                raise LiveHlsError(503, "live_hls_configuration_unavailable") from None
            self.last_config = rendered
            self.sessions.clear()
            self.startup_error = None
            logger.info("Updated private live HLS paths: %s", len(rendered["paths"]))

    async def _read(
        self, camera: str, height: int, resource: str, params: dict[str, str]
    ) -> bytes:
        """Read a bounded resource from a fixed loopback origin without redirects."""
        if self.client is None:
            raise LiveHlsError(503, "live_hls_not_started")
        url = resource_url(camera, height, resource)
        await self.sync_config()
        try:
            async with self.client.stream(
                "GET", url, params=params, headers={"Accept-Encoding": "identity"}
            ) as response:
                if response.status_code == 404:
                    raise LiveHlsError(404, "live_hls_resource_not_found")
                if response.status_code == 401:
                    raise LiveHlsError(401, "live_hls_private_session_expired")
                if response.status_code != 200:
                    logger.error(
                        "Live HLS upstream failed for %s with status %s",
                        camera,
                        response.status_code,
                    )
                    raise LiveHlsError(502, "live_hls_upstream_error")
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(data) + len(chunk) > MAX_RESOURCE_BYTES:
                        logger.error(
                            "Live HLS resource exceeded its size limit for %s", camera
                        )
                        raise LiveHlsError(502, "live_hls_resource_too_large")
                    data.extend(chunk)
        except httpx.TimeoutException:
            logger.warning("Live HLS upstream timed out for %s", camera)
            raise LiveHlsError(504, "live_hls_upstream_timeout") from None
        except httpx.RequestError:
            logger.warning("Live HLS packager is unavailable for %s", camera)
            raise LiveHlsError(503, "live_hls_packager_unavailable") from None
        return bytes(data)

    async def bootstrap(
        self, camera: str, height: int, expired: PackagerMaster | None = None
    ) -> PackagerMaster:
        """Create one shared upstream session per authorized camera rendition."""
        key = (camera, height)
        lock = self.session_locks.setdefault(key, asyncio.Lock())
        async with lock:
            current = self.sessions.get(key)
            if current is not None and (expired is None or current is not expired):
                return current
            try:
                # The pinned HTTP packager needs this to use query sessions
                # without its Secure-cookie capability-check redirect.
                data = await self._read(
                    camera, height, "index.m3u8", {"cookieCheck": "1"}
                )
            except LiveHlsError as error:
                if error.status != 401:
                    raise
                logger.error("Private live HLS bootstrap was rejected for %s", camera)
                raise LiveHlsError(502, "live_hls_bootstrap_rejected") from None
            try:
                master = parse_packager_master(data.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                logger.error("Invalid private live HLS bootstrap for %s", camera)
                raise LiveHlsError(502, "live_hls_invalid_manifest") from None
            self.sessions[key] = master
            return master

    async def master(
        self,
        camera: str,
        renditions: tuple[LiveHlsRendition, ...],
        *,
        audio: bool = True,
    ) -> str:
        """Build a shared, authenticated bitrate ladder with real codec metadata."""
        await self.sync_config()
        masters = await asyncio.gather(
            *(self.bootstrap(camera, value.height) for value in renditions)
        )
        return master_playlist(
            tuple(zip(renditions, masters, strict=True)), audio=audio
        )

    async def fetch(
        self, camera: str, height: int, resource: str, params: dict[str, str]
    ) -> bytes:
        """Keep packager sessions behind the camera-authenticated proxy."""
        if resource == "index.m3u8":
            raise LiveHlsError(404, "live_hls_resource_not_found")
        await self.sync_config()
        master = await self.bootstrap(camera, height)
        try:
            data = await self._read(
                camera, height, resource, {**params, "session": master.session}
            )
        except LiveHlsError as error:
            missing_playlist = error.status == 404 and resource in (
                master.video,
                master.audio,
            )
            if error.status != 401 and not missing_playlist:
                raise
            master = await self.bootstrap(camera, height, expired=master)
            # A recreated muxer starts a new sequence; old blocking reload
            # positions can otherwise wait for segments from its previous lifetime.
            retry_params = {} if resource.endswith(".m3u8") else params
            try:
                data = await self._read(
                    camera,
                    height,
                    resource,
                    {**retry_params, "session": master.session},
                )
            except LiveHlsError as retry_error:
                if retry_error.status != 401:
                    raise
                logger.error(
                    "Private live HLS session could not be renewed for %s", camera
                )
                raise LiveHlsError(
                    502, "live_hls_private_session_unavailable"
                ) from None
        if resource.endswith(".m3u8"):
            try:
                return public_media_playlist(data.decode("utf-8")).encode()
            except (UnicodeDecodeError, ValueError):
                logger.error("Invalid private live HLS media playlist for %s", camera)
                raise LiveHlsError(502, "live_hls_invalid_manifest") from None
        return data

    async def close(self) -> None:
        """Release local connections when the Frigate API stops."""
        if self.client is not None:
            await self.client.aclose()
            self.client = None
        self.sessions.clear()
        self.session_locks.clear()
