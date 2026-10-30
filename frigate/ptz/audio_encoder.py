"""Explicit Media1/Media2 audio encoder reads and verified serialized writes."""

import httpx
from lxml.etree import Error as XmlError
from onvif import ONVIFError
from zeep.exceptions import Error as ZeepError

from frigate.ptz.connection import camera_session, create_camera_service
from frigate.ptz.imaging import ImagingWriteUnverified
from frigate.util.onvif_audio import apply_audio_changes, audio_state
from frigate.util.onvif_device import device_value


async def _read(service, profile_token, kind="media"):
    raw = await service.GetProfiles() or []
    profiles = []
    for item in raw:
        token = device_value(item, "token")
        if not isinstance(token, str) or not token:
            raise ValueError("The camera reported an invalid media profile")
        bindings = device_value(item, "Configurations") if kind == "media2" else item
        profiles.append(
            {
                "token": token,
                "name": device_value(item, "Name") or token,
                "encoder_token": device_value(
                    bindings,
                    "AudioEncoder" if kind == "media2" else "AudioEncoderConfiguration",
                    "token",
                ),
                "audio_source_token": device_value(
                    bindings,
                    "AudioSource" if kind == "media2" else "AudioSourceConfiguration",
                    "SourceToken",
                ),
            }
        )
    response = {
        "profiles": profiles,
        "profile_token": profile_token,
        "state": "choose_profile" if profiles else "unsupported",
        "encoder": None,
    }
    if profile_token is None:
        return response
    selected = next((item for item in profiles if item["token"] == profile_token), None)
    if selected is None:
        raise ValueError("Choose a reported audio media profile")
    if not selected["encoder_token"] or not selected["audio_source_token"]:
        return {**response, "state": "unsupported"}
    parameters = {
        "ConfigurationToken": selected["encoder_token"],
        "ProfileToken": profile_token,
    }
    if kind == "media2":
        configurations = await service.GetAudioEncoderConfigurations(parameters)
        configuration = next(
            (
                item
                for item in configurations or []
                if device_value(item, "token") == selected["encoder_token"]
            ),
            None,
        )
    else:
        configuration = await service.GetAudioEncoderConfiguration(
            {"ConfigurationToken": selected["encoder_token"]}
        )
    if device_value(configuration, "token") != selected["encoder_token"]:
        raise ValueError("The camera did not return the selected audio encoder")
    options = await service.GetAudioEncoderConfigurationOptions(parameters)
    shared = [
        item["token"]
        for item in profiles
        if item["encoder_token"] == selected["encoder_token"]
    ]
    state = audio_state(configuration, options, shared, selected["audio_source_token"])
    state["persistence_required"] = kind == "media2"
    return {
        **response,
        "state": "ready" if state["editable"] else "read_only",
        "encoder": state,
        "_configuration": configuration,
    }


async def read_audio_encoder(connection, *, kind="media"):
    """Read only this saved camera's explicitly selected audio configuration."""
    async with camera_session(connection) as camera:
        service = await create_camera_service(camera, kind, connection)
        response = await _read(service, connection.profile_token, kind)
        response.pop("_configuration", None)
        return response


async def write_audio_encoder(
    connection,
    changes,
    expected_revision,
    *,
    force_persistence=False,
    before_write=None,
    kind="media",
):
    """Apply once after fresh option/revision checks, then verify the full target."""
    async with camera_session(connection) as camera:
        service = await create_camera_service(camera, kind, connection)
        response = await _read(service, connection.profile_token, kind)
        state = response["encoder"]
        if state is None:
            raise ValueError(
                "Choose a profile with a reported audio source and encoder"
            )
        updated = apply_audio_changes(
            response["_configuration"], state, changes, expected_revision
        )
        if kind == "media2" and not force_persistence:
            raise ValueError("Media2 audio writes require explicit device persistence")
        if before_write is not None:
            before_write()
        try:
            request = {"Configuration": updated}
            if kind == "media":
                request["ForcePersistence"] = force_persistence
            await service.SetAudioEncoderConfiguration(request)
            observed = await _read(service, connection.profile_token, kind)
        except (
            ONVIFError,
            ZeepError,
            XmlError,
            httpx.HTTPError,
            TimeoutError,
            OSError,
            ValueError,
        ) as error:
            raise ImagingWriteUnverified(
                "Audio encoder change could not be verified"
            ) from error
        actual = observed["encoder"]
        if actual is None or any(
            actual[key] != device_value(updated, field)
            for key, field in (
                ("encoding", "Encoding"),
                ("bitrate_kbps", "Bitrate"),
                ("sample_rate_khz", "SampleRate"),
            )
        ):
            raise ImagingWriteUnverified("The camera did not confirm the audio encoder")
        observed.pop("_configuration", None)
        return observed
