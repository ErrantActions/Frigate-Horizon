"""Ring cloud adapter for Horizon's JSON-lines plugin protocol."""

import asyncio
import base64
import json
import logging
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode


class RingPlugin:
    """Keep account credentials out of YAML and worker command lines."""

    def __init__(self, data: Path) -> None:
        self.data = data
        self.auth = None
        self.ring = None
        self.token = {}
        self.seen = {}
        self.last_inventory = 0.0
        self.state = "auth_required"

    def save(self, token):
        """Persist rotating tokens atomically with owner-only permissions."""
        self.token = token
        self.data.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, name = tempfile.mkstemp(dir=self.data, prefix=".token-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(token, output)
            os.replace(name, self.data / "token.json")
        finally:
            if os.path.exists(name):
                os.unlink(name)

    async def connect(self):
        """Restore a session using the cached token, never a saved password."""
        from ring_doorbell import Auth, Ring

        if self.auth is not None:
            return
        self.data.mkdir(parents=True, exist_ok=True, mode=0o700)
        identity = self.data / "hardware-id"
        if not identity.exists():
            identity.write_text(str(uuid.uuid4()))
            identity.chmod(0o600)
        hardware_id = str(uuid.UUID(identity.read_text().strip()))
        token_file = self.data / "token.json"
        self.token = json.loads(token_file.read_text()) if token_file.exists() else {}
        self.auth = Auth(
            "FrigateHorizon/1.0",
            self.token,
            self.save,
            hardware_id=hardware_id,
        )
        self.ring = Ring(self.auth)

    async def authenticate(self, params):
        """Request the account's 2FA flow without persisting its password."""
        from ring_doorbell import AuthenticationError, Requires2FAError

        await self.connect()
        try:
            await self.auth.async_fetch_token(
                params["email"], params["password"], params.get("code") or None
            )
        except Requires2FAError:
            self.state = "two_factor_required"
            return {"state": self.state}
        except AuthenticationError:
            self.state = "auth_required"
            return {"state": self.state}
        self.ring.session = None
        self.last_inventory = 0
        return await self.poll()

    async def poll(self):
        """Refresh devices periodically and deduplicate Ring's active-ding feed."""
        from ring_doorbell import AuthenticationError

        await self.connect()
        if not self.token:
            return {"state": self.state, "devices": [], "events": []}
        try:
            if time.monotonic() - self.last_inventory >= 60:
                await self.ring.async_update_devices()
                self.last_inventory = time.monotonic()
            await self.ring.async_update_dings()
        except AuthenticationError:
            self.state = "auth_required"
            return {"state": self.state, "devices": [], "events": []}
        devices = []
        for kind in ("doorbots", "authorized_doorbots", "stickup_cams"):
            for device in self.ring.devices_data.get(kind, {}).values():
                if not device.get("device_id"):
                    continue
                query = urlencode(
                    {
                        "camera_id": device["id"],
                        "device_id": device["device_id"],
                        "refresh_token": base64.b64encode(
                            json.dumps(
                                {
                                    "rt": self.token["refresh_token"],
                                    "hid": self.auth.get_hardware_id(),
                                },
                                separators=(",", ":"),
                            ).encode()
                        ).decode(),
                    }
                )
                devices.append(
                    {
                        "id": str(device["id"]),
                        "name": device.get("description", str(device["id"])),
                        "source": "ring:?" + query,
                        "two_way_audio": True,
                    }
                )
        now = time.time()
        self.seen = {key: expiry for key, expiry in self.seen.items() if expiry > now}
        events = []
        for ding in self.ring.dings_data:
            key = str(ding["id"])
            if key in self.seen:
                continue
            self.seen[key] = now + 3600
            kind = ding.get("kind")
            if kind not in ("motion", "ding"):
                continue
            events.append(
                {
                    "id": key,
                    "device_id": str(ding["doorbot_id"]),
                    "kind": "doorbell" if kind == "ding" else "motion",
                    "timestamp": now,
                }
            )
        self.state = "running"
        return {"state": self.state, "devices": devices, "events": events}

    async def handle(self, method, params):
        """Dispatch only protocol actions; package installation never executes hooks."""
        if method == "authenticate":
            return await self.authenticate(params)
        if method == "poll":
            return await self.poll()
        if method == "disconnect":
            if self.auth:
                await self.auth.async_close()
            self.auth = self.ring = None
            self.token = {}
            (self.data / "token.json").unlink(missing_ok=True)
            self.state = "auth_required"
            return {"state": self.state}
        raise ValueError("Unknown plugin method")


async def main():
    """Process one request at a time and return no external exception details."""
    plugin = RingPlugin(Path(os.environ["HORIZON_PLUGIN_DATA"]))
    try:
        while line := await asyncio.to_thread(sys.stdin.readline):
            request_id = 0
            try:
                request = json.loads(line)
                request_id = request["id"]
                result = await plugin.handle(
                    request["method"], request.get("params", {})
                )
                reply = {"id": request_id, "result": result}
            except Exception as error:
                # This is the plugin-process boundary. Never return credential-bearing errors.
                reply = {"id": request_id, "error": type(error).__name__}
            print(json.dumps(reply), flush=True)
    finally:
        if plugin.auth:
            await plugin.auth.async_close()


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    asyncio.run(main())
