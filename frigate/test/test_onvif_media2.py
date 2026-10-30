"""Media2 schema and SOAP regression tests using an in-memory HTTP transport."""

import copy
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from lxml import etree

from frigate.api.defs.request.onvif_body import OnvifConnectionBody, OnvifEncoderChanges
from frigate.ptz.encoder import media2_state, read_encoder, write_encoder
from frigate.ptz.imaging import ImagingWriteUnverified
from frigate.ptz.media2 import NAMESPACE, SCHEMA, SOAP, Media2Service, _decode

CONFIGURATION = f"""
<tr2:Configurations xmlns:tr2="{NAMESPACE}" xmlns:tt="{SCHEMA}"
 token="encoder-2" GovLength="30" Profile="Main" vendor="preserve">
 <tt:Name>Main encoder</tt:Name><tt:UseCount>2</tt:UseCount>
 <tt:Encoding>H265</tt:Encoding>
 <tt:Resolution><tt:Width>3840</tt:Width><tt:Height>2160</tt:Height></tt:Resolution>
 <tt:RateControl><tt:FrameRateLimit>15</tt:FrameRateLimit><tt:BitrateLimit>4096</tt:BitrateLimit></tt:RateControl>
 <tt:Quality>4</tt:Quality><tt:VendorExtension>untouched</tt:VendorExtension>
</tr2:Configurations>"""
OPTIONS = f"""
<tr2:Options xmlns:tr2="{NAMESPACE}" xmlns:tt="{SCHEMA}"
 GovLengthRange="1 120" FrameRatesSupported="30 15 7.5" ProfilesSupported="Main">
 <tt:Encoding>H265</tt:Encoding>
 <tt:QualityRange><tt:Min>1</tt:Min><tt:Max>6</tt:Max></tt:QualityRange>
 <tt:ResolutionsAvailable><tt:Width>3840</tt:Width><tt:Height>2160</tt:Height></tt:ResolutionsAvailable>
 <tt:BitrateRange><tt:Min>512</tt:Min><tt:Max>16384</tt:Max></tt:BitrateRange>
</tr2:Options>"""


class TestMedia2(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.connection = OnvifConnectionBody(
            host="camera.invalid", profile_token="main2"
        )
        self.configuration = _decode(etree.fromstring(CONFIGURATION))
        self.configuration["_xml"] = etree.fromstring(CONFIGURATION)
        self.options = [_decode(etree.fromstring(OPTIONS))]
        self.service = SimpleNamespace(
            GetProfiles=AsyncMock(
                return_value=[
                    {
                        "token": "main2",
                        "Name": "Main",
                        "Configurations": {
                            "VideoEncoder": {"token": "encoder-2"},
                            "VideoSource": {"SourceToken": "sensor"},
                        },
                    }
                ]
            ),
            GetVideoEncoderConfigurations=AsyncMock(return_value=[self.configuration]),
            GetVideoEncoderConfigurationOptions=AsyncMock(return_value=self.options),
            SetVideoEncoderConfiguration=AsyncMock(),
        )

        @asynccontextmanager
        async def session(connection):
            yield object()

        self.session = patch("frigate.ptz.encoder.camera_session", session)
        self.services = patch(
            "frigate.ptz.encoder.create_camera_service",
            AsyncMock(return_value=self.service),
        )
        self.session.start()
        self.services.start()
        self.addCleanup(self.session.stop)
        self.addCleanup(self.services.stop)

    async def test_media2_h265_options_are_explicit_and_discrete(self):
        result = await read_encoder(self.connection, "media2")
        state = result["encoder"]
        self.assertEqual(state.encoding, "H265")
        self.assertTrue(state.editable)
        self.assertTrue(state.persistence_required)
        self.assertEqual(state.options.gop_length.maximum, 120)
        self.assertEqual(state.frame_rates, [30, 15, 7.5])
        self.assertIsNone(state.options.frame_rate)
        self.assertIsNone(state.options.encoding_interval)

    async def test_media2_never_silently_requests_persistence(self):
        state = media2_state(self.configuration, self.options, ["main2"])
        with self.assertRaises(ValueError):
            await write_encoder(
                self.connection,
                "media2",
                OnvifEncoderChanges(frame_rate=7.5),
                state.revision,
            )
        self.service.SetVideoEncoderConfiguration.assert_not_awaited()

    async def test_unreported_discrete_frame_rate_is_rejected(self):
        state = media2_state(self.configuration, self.options, ["main2"])
        with self.assertRaises(ValueError):
            await write_encoder(
                self.connection,
                "media2",
                OnvifEncoderChanges(frame_rate=10),
                state.revision,
                force_persistence=True,
            )
        self.service.SetVideoEncoderConfiguration.assert_not_awaited()

    async def test_h265_update_verifies_readback_and_preserves_unknown_fields(self):
        state = media2_state(self.configuration, self.options, ["main2"])
        observed = copy.deepcopy(self.configuration)
        observed["RateControl"]["FrameRateLimit"] = 7.5
        observed["GovLength"] = 60
        self.service.GetVideoEncoderConfigurations.side_effect = [
            [self.configuration],
            [observed],
        ]
        result = await write_encoder(
            self.connection,
            "media2",
            OnvifEncoderChanges(frame_rate=7.5, gop_length=60),
            state.revision,
            force_persistence=True,
        )
        self.assertEqual(result["encoder"].frame_rate, 7.5)
        request = self.service.SetVideoEncoderConfiguration.await_args.args[0]
        self.assertNotIn("ForcePersistence", request)
        self.assertEqual(request["Configuration"]["vendor"], "preserve")
        self.assertEqual(self.configuration["RateControl"]["FrameRateLimit"], 15)

    async def test_lost_encoder_acknowledgement_is_not_retried(self):
        state = media2_state(self.configuration, self.options, ["main2"])
        self.service.SetVideoEncoderConfiguration.side_effect = httpx.ReadTimeout(
            "mock"
        )
        with self.assertRaises(ImagingWriteUnverified):
            await write_encoder(
                self.connection,
                "media2",
                OnvifEncoderChanges(gop_length=60),
                state.revision,
                force_persistence=True,
            )
        self.service.SetVideoEncoderConfiguration.assert_awaited_once()

    async def test_adapter_writes_gop_as_attribute_not_a_fabricated_element(self):
        camera = SimpleNamespace(encrypt=True, dt_diff=None)
        service = Media2Service("http://camera.invalid/media2", self.connection, camera)
        self.addAsyncCleanup(service.close)
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(
                200,
                content=f'<s:Envelope xmlns:s="{SOAP}"><s:Body><SetVideoEncoderConfigurationResponse xmlns="{NAMESPACE}"/></s:Body></s:Envelope>',
            )

        await service._client.aclose()
        service._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        configuration = copy.deepcopy(self.configuration)
        configuration["GovLength"] = 60
        await service.SetVideoEncoderConfiguration({"Configuration": configuration})
        content = requests[0].content
        root = etree.fromstring(content)
        changed = root.find(f".//{{{NAMESPACE}}}Configuration")
        self.assertEqual(changed.get("GovLength"), "60")
        self.assertIsNone(changed.find(f"{{{SCHEMA}}}GovLength"))
        self.assertEqual(changed.find(f"{{{SCHEMA}}}VendorExtension").text, "untouched")
        self.assertNotIn(b"ForcePersistence", content)
