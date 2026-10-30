import asyncio
import hashlib
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from joserfc.jwk import OctKey
from playhouse.sqliteq import SqliteQueueDatabase
from pydantic import ValidationError
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette_context import middleware, plugins
from starlette_context.plugins import Plugin

from frigate.api import (
    adaptive_motion,
    audio_repair,
    auth,
    bookmarks,
    camera,
    chat,
    classification,
    compressed_motion,
    compression,
    debug_replay,
    diagnostics,
    environment,
    event,
    export,
    hardware,
    media,
    motion_search,
    notices,
    notification,
    playback,
    preview,
    protected_media,
    record,
    review,
)
from frigate.api import app as main_app
from frigate.api import (
    native as native_api,
)
from frigate.api import (
    onvif as onvif_api,
)
from frigate.api import (
    plugins as plugins_api,
)
from frigate.api.auth import get_jwt_secret, limiter, require_admin_by_default
from frigate.comms.dispatcher import Dispatcher
from frigate.comms.event_metadata_updater import (
    EventMetadataPublisher,
)
from frigate.comms.reolink_api import ReolinkEventManager
from frigate.config import FrigateConfig
from frigate.config.camera.updater import CameraConfigUpdatePublisher
from frigate.config.holder import ConfigHolder
from frigate.config.profile_manager import ProfileManager
from frigate.const import CONFIG_DIR
from frigate.debug_replay import DebugReplayManager, debug_replay_auto_stop_watchdog
from frigate.embeddings import EmbeddingsContext
from frigate.environment.runtime import EnvironmentManager
from frigate.genai import GenAIClientManager
from frigate.motion.compressed_preview import VectorPreviewManager
from frigate.motion.timing_calibration import TemporaryTimingTuner
from frigate.notices.registry import NoticeRegistry
from frigate.plugins.manager import PluginManager
from frigate.ptz.connection import drain_camera_cleanup
from frigate.ptz.device_manager import OnvifDeviceManager
from frigate.ptz.onvif import OnvifController
from frigate.ptz.replay import OnvifReplayManager
from frigate.record.audio_repair_manager import AudioRepairManager
from frigate.record.compression_manager import CompressionManager
from frigate.record.playback import PlaybackManager
from frigate.stats.emitter import StatsEmitter
from frigate.storage import StorageMaintainer

logger = logging.getLogger(__name__)


def check_csrf(request: Request) -> bool:
    if request.method in ["GET", "HEAD", "OPTIONS", "TRACE"]:
        return True
    if "origin" in request.headers and "x-csrf-token" not in request.headers:
        return False

    return True


# Used to retrieve the remote-user header: https://starlette-context.readthedocs.io/en/latest/plugins.html#easy-mode
class RemoteUserPlugin(Plugin):
    key = "Remote-User"


def create_fastapi_app(
    frigate_config: FrigateConfig,
    database: SqliteQueueDatabase,
    embeddings: EmbeddingsContext | None,
    detected_frames_processor,
    storage_maintainer: StorageMaintainer,
    onvif: OnvifController,
    stats_emitter: StatsEmitter,
    event_metadata_updater: EventMetadataPublisher,
    config_publisher: CameraConfigUpdatePublisher,
    replay_manager: DebugReplayManager,
    dispatcher: Dispatcher | None = None,
    profile_manager: ProfileManager | None = None,
    enforce_default_admin: bool = True,
    config_holder: ConfigHolder | None = None,
    notice_registry: NoticeRegistry | None = None,
    record_cleanup=None,
    event_cleanup=None,
    environment_manager: EnvironmentManager | None = None,
):
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info("FastAPI started")
        devices_started = False
        replay_watchdog = None
        try:
            # Frigate initializes its database/models before entering ASGI startup.
            await app.playback_manager.start()
            await app.state.audio_repair_manager.start()
            await app.state.compression_manager.start()
            await asyncio.to_thread(
                app.state.vector_previews.start_background, frigate_config.cameras
            )
            app.onvif_devices.start()
            app.state.reolink_events.start()
            devices_started = True
            try:
                await app.state.plugins.start()
            except (OSError, ValueError, ValidationError):
                logger.error(
                    "Plugin state could not be loaded; existing camera services remain active"
                )
                app.state.plugins_error = "invalid_plugin_state"
            await asyncio.to_thread(app.onvif_devices._ready.wait, 5)
            app.state.native_health.start()
            replay_watchdog = asyncio.create_task(
                debug_replay_auto_stop_watchdog(
                    replay_manager, frigate_config, config_publisher
                )
            )
            yield
        finally:
            try:
                try:
                    await app.state.native_health.close()
                finally:
                    try:
                        await app.state.native_talkback.close()
                    finally:
                        await app.state.native_controls.close()
            finally:
                try:
                    await app.state.plugins.close()
                finally:
                    if replay_watchdog is not None:
                        replay_watchdog.cancel()
                        await asyncio.gather(replay_watchdog, return_exceptions=True)
                    try:
                        try:
                            try:
                                try:
                                    try:
                                        try:
                                            await asyncio.to_thread(
                                                app.state.reolink_events.close
                                            )
                                        finally:
                                            await asyncio.to_thread(
                                                app.state.timing_calibration.close
                                            )
                                    finally:
                                        await asyncio.to_thread(
                                            app.state.vector_previews.close
                                        )
                                finally:
                                    await app.state.compression_manager.close()
                            finally:
                                await app.state.audio_repair_manager.close()
                        finally:
                            await app.playback_manager.close()
                    finally:
                        try:
                            await app.onvif_replay.shutdown()
                        finally:
                            if devices_started:
                                app.onvif_devices.stop()
                                await asyncio.to_thread(app.onvif_devices.join, 15)
                                if app.onvif_devices.is_alive():
                                    logger.error(
                                        "ONVIF device ownership service exceeded its shutdown deadline"
                                    )
                            await drain_camera_cleanup()

    logger.info("Starting FastAPI app")
    app = FastAPI(
        debug=False,
        lifespan=lifespan,
        swagger_ui_parameters={"apisSorter": "alpha", "operationsSorter": "alpha"},
        dependencies=[Depends(require_admin_by_default())]
        if enforce_default_admin
        else [],
    )
    app.state.vector_previews = VectorPreviewManager()
    app.state.timing_calibration = TemporaryTimingTuner(frigate_config.cameras)
    app.state.reolink_events = ReolinkEventManager(lambda: app.frigate_config)
    app.state.plugins = PluginManager(
        Path(CONFIG_DIR) / "plugins",
        lambda: app.frigate_config,
        lambda payload: plugins_api.publish_plugin_event(app, payload),
    )

    # update the request_address with the x-forwarded-for header from nginx
    # https://starlette-context.readthedocs.io/en/latest/plugins.html#forwarded-for
    app.add_middleware(
        middleware.ContextMiddleware,
        plugins=(plugins.ForwardedForPlugin(),),
    )

    # Middleware to connect to DB before and close connection after request
    # https://github.com/fastapi/full-stack-fastapi-template/issues/224#issuecomment-737423886
    # https://fastapi.tiangolo.com/tutorial/middleware/#before-and-after-the-response
    @app.middleware("http")
    async def frigate_middleware(request: Request, call_next):
        # Before request
        if not check_csrf(request):
            return JSONResponse(
                content={"success": False, "message": "Missing CSRF header"},
                status_code=401,
            )

        if database.is_closed():
            database.connect()

        response = await call_next(request)

        # After request https://stackoverflow.com/a/75487519
        if not database.is_closed():
            database.close()
        return response

    # Rate limiter (used for login endpoint)
    if frigate_config.auth.failed_login_rate_limit is None:
        limiter.enabled = False
    else:
        auth.rateLimiter.set_limit(frigate_config.auth.failed_login_rate_limit)

    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIMiddleware)

    # Routes
    # Order of include_router matters: https://fastapi.tiangolo.com/tutorial/path-params/#order-matters
    app.include_router(auth.router)
    app.include_router(camera.router)
    app.include_router(plugins_api.router)
    app.include_router(adaptive_motion.router)
    app.include_router(compressed_motion.router)
    app.include_router(chat.router)
    app.include_router(classification.router)
    app.include_router(review.router)
    app.include_router(main_app.router)
    app.include_router(diagnostics.router)
    app.include_router(preview.router)
    app.include_router(notification.router)
    app.include_router(native_api.router)
    app.include_router(export.router)
    app.include_router(hardware.router)
    app.include_router(notices.router)
    app.include_router(event.router)
    app.include_router(environment.router)
    app.include_router(onvif_api.router)
    app.include_router(playback.router)
    app.include_router(audio_repair.router)
    app.include_router(protected_media.router)
    app.include_router(bookmarks.router)
    app.include_router(compression.router)
    app.include_router(media.router)
    app.include_router(motion_search.router)
    app.include_router(record.router)
    app.include_router(debug_replay.router)
    # App Properties
    app.frigate_config = frigate_config
    # snapshot the port nginx bound at startup, the live config can be swapped
    app.auth_internal_port = frigate_config.networking.listen.internal_port
    app.genai_manager = GenAIClientManager(frigate_config)
    app.embeddings = embeddings
    app.detected_frames_processor = detected_frames_processor
    app.storage_maintainer = storage_maintainer
    app.record_cleanup = record_cleanup
    app.event_cleanup = event_cleanup
    app.camera_error_image = None
    app.onvif = onvif
    app.stats_emitter = stats_emitter
    app.notice_registry = notice_registry
    app.event_metadata_updater = event_metadata_updater
    app.config_publisher = config_publisher
    app.replay_manager = replay_manager
    app.dispatcher = dispatcher
    app.profile_manager = profile_manager
    app.environment_manager = environment_manager
    app.onvif_devices = OnvifDeviceManager(
        lambda: app.frigate_config, Path(CONFIG_DIR) / "onvif-device-state.json"
    )
    app.onvif_replay = OnvifReplayManager(lambda: app.frigate_config)
    app.playback_manager = PlaybackManager(
        frigate_config.ffmpeg.ffmpeg_path,
        get_hwaccel_args=lambda camera: (
            app.frigate_config.cameras[camera].ffmpeg.hwaccel_args
        ),
        compatibility_enabled=lambda: app.frigate_config.ui.recording_compatibility,
    )
    app.state.audio_repair_manager = AudioRepairManager(lambda: app.frigate_config)
    app.state.compression_manager = CompressionManager(
        lambda: app.frigate_config, readers_leased=False
    )
    if environment_manager is not None:
        environment_manager.device_manager = app.onvif_devices
    app.config_holder = config_holder

    if frigate_config.auth.enabled:
        secret = get_jwt_secret()
        key_bytes = None
        if isinstance(secret, str):
            # If the secret looks like hex (e.g., generated by secrets.token_hex), use raw bytes
            if len(secret) % 2 == 0 and re.fullmatch(r"[0-9a-fA-F]+", secret or ""):
                try:
                    key_bytes = bytes.fromhex(secret)
                except ValueError:
                    key_bytes = secret.encode("utf-8")
            else:
                key_bytes = secret.encode("utf-8")
        elif isinstance(secret, (bytes, bytearray)):
            key_bytes = bytes(secret)
        else:
            key_bytes = str(secret).encode("utf-8")

        app.jwt_token = OctKey.import_key(key_bytes)
    else:
        app.jwt_token = None
        secret = get_jwt_secret()

    # Reuse the persisted instance secret without exposing its material through
    # source references, including source IDs containing vendor credentials.
    source_secret = secret if isinstance(secret, bytes) else str(secret).encode()
    app.onvif_source_key = (
        hashlib.sha256(b"onvif-recording-source-key-v1\0" + source_secret).digest()
        if len(source_secret) >= 32
        else None
    )

    native_api.initialize_native_controls(app)
    from frigate.comms.native_health import NativeHealthMonitor

    app.state.native_health = NativeHealthMonitor(app)
    # New protection requires verified recovery that also preserves protected assets.
    app.state.native_export_protection = False
    return app
