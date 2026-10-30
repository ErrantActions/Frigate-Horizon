"""Real offline Media2 WSDL and strict schema/result safety tests, without SDK or cameras."""

import asyncio
import base64
import unittest
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from lxml import etree
from zeep import AsyncClient, Settings, xsd
from zeep.transports import AsyncTransport
from zeep.wsse.username import UsernameToken

from frigate.ptz.wsdl_service import (
    MEDIA2_BINDING,
    SCHEMA_ROOT,
    OfflineTransport,
    SchemaService,
    media2_service,
)
from frigate.util.onvif_schema import (
    describe_operations,
    invoke_bound_operation,
    pinned_service_url,
    safe_result,
    schema_for_type,
    schema_ready,
    validate_parameters,
)


class TestOfficialMedia2Schema(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.transport = OfflineTransport()
        cls.client = AsyncClient(
            str(
                SCHEMA_ROOT
                / "www.onvif.org"
                / "ver20"
                / "media"
                / "wsdl"
                / "media.wsdl"
            ),
            transport=cls.transport,
            settings=Settings(forbid_dtd=True, forbid_entities=True),
        )
        cls.binding = cls.client.wsdl.bindings[MEDIA2_BINDING]

    @classmethod
    def tearDownClass(cls):
        asyncio.run(cls.transport.aclose())

    def test_official_binding_includes_audio_metadata_and_profiles_not_only_video(self):
        operations = self.binding.all()
        for name in (
            "GetProfiles",
            "CreateProfile",
            "DeleteProfile",
            "GetAudioEncoderConfigurations",
            "SetAudioEncoderConfiguration",
            "GetAudioEncoderConfigurationOptions",
            "GetMetadataConfigurations",
            "GetOSDs",
            "GetStreamUri",
            "SetVideoEncoderConfiguration",
        ):
            self.assertIn(name, operations)
        self.assertGreater(len(operations), 50)

    def test_no_remote_or_unbundled_schema_can_be_loaded(self):
        for url in (
            "https://other.invalid/schema.xsd",
            "https://www.onvif.org/unknown.xsd",
        ):
            with self.assertRaises(ValueError):
                self.transport.load(url)

    def test_bundle_integrity_is_checked_without_mutating_licensed_schemas(self):
        with patch("pathlib.Path.read_bytes", return_value=b"tampered"):
            with self.assertRaises(ValueError):
                self.transport.load(
                    str(
                        SCHEMA_ROOT
                        / "www.onvif.org"
                        / "ver20"
                        / "media"
                        / "wsdl"
                        / "media.wsdl"
                    )
                )

    def test_get_profiles_uses_real_type_enum_list(self):
        operation = self.binding.get("GetProfiles")
        schema = schema_for_type(operation.input.body.type)
        parsed = validate_parameters(schema, {"Type": ["All"]})
        self.assertEqual(parsed, {"Type": ["All"]})

    def test_every_real_media2_operation_can_be_cataloged_without_invented_fields(self):
        service = SimpleNamespace(zeep_client=self.client, binding_name=MEDIA2_BINDING)
        operations = describe_operations(
            service, {"GetProfiles"}, set(), set(), set(), "media2"
        )
        self.assertEqual(len(operations), len(self.binding.all()))
        by_name = {item["name"]: item for item in operations}
        self.assertEqual(by_name["GetProfiles"]["effect"], "read")
        self.assertEqual(by_name["GetProfiles"]["readiness"], "ready")
        self.assertEqual(by_name["DeleteProfile"]["effect"], "mutating")
        self.assertTrue(by_name["DeleteProfile"]["typed_confirmation"])
        self.assertEqual(by_name["GetProfiles"]["device_support"], "unverified")
        self.assertEqual(by_name["AddAudioClip"]["readiness"], "file_transfer_required")
        self.assertEqual(
            by_name["AddAudioClip"]["transfer_protocol"], "device_upload_uri"
        )
        self.assertTrue(schema_ready(by_name["SetVideoEncoderConfiguration"]["schema"]))
        configuration = by_name["SetVideoEncoderConfiguration"]["schema"]["properties"][
            "Configuration"
        ]
        self.assertIn("token", configuration["properties"])
        self.assertIn("GovLength", configuration["properties"])


class TestCertificateSchemas(unittest.IsolatedAsyncioTestCase):
    def operation_catalog(self, operation_name, service_name="devicemgmt"):
        certificate = xsd.ComplexType(
            [
                xsd.Element("CertificateID", xsd.String()),
                xsd.Element("Certificate", xsd.Base64Binary()),
            ]
        )
        body = xsd.Element(
            operation_name,
            xsd.ComplexType(
                [xsd.Element("Certificate", certificate, max_occurs="unbounded")]
            ),
        )
        operation = SimpleNamespace(
            input=SimpleNamespace(body=body), output=SimpleNamespace(body=None)
        )
        binding = SimpleNamespace(
            all=lambda: {operation_name: operation},
            get=lambda _name: operation,
        )
        invoke = unittest.mock.AsyncMock(return_value=None)
        service = SimpleNamespace(
            binding_name="fixture",
            zeep_client=SimpleNamespace(
                wsdl=SimpleNamespace(bindings={"fixture": binding})
            ),
            ws_client={operation_name: invoke},
        )
        catalog = describe_operations(service, set(), set(), set(), set(), service_name)
        return service, catalog[0], invoke

    async def test_certificate_upload_requires_typed_consent_and_sends_bytes_once(self):
        for name in (
            "LoadCertificates",
            "LoadCACertificates",
            "LoadCertificateWithPrivateKey",
        ):
            with self.subTest(operation=name):
                service, operation, invoke = self.operation_catalog(name)
                self.assertEqual(operation["readiness"], "ready")
                self.assertTrue(operation["binary_input"])
                parameters = {
                    "Certificate": [
                        {
                            "CertificateID": "reviewed-certificate",
                            "Certificate": "data:application/octet-stream;base64,"
                            + base64.b64encode(b"synthetic-certificate").decode(),
                        }
                    ]
                }
                guard = Mock()
                with self.assertRaises(ValueError):
                    await invoke_bound_operation(
                        service, operation, parameters, "", guard
                    )
                invoke.assert_not_awaited()
                guard.assert_not_called()
                response = await invoke_bound_operation(
                    service, operation, parameters, name, guard
                )
                self.assertEqual(response["state"], "acknowledged_unverified")
                invoke.assert_awaited_once()
                self.assertEqual(
                    invoke.call_args.kwargs["Certificate"][0]["Certificate"],
                    b"synthetic-certificate",
                )
                guard.assert_called_once()

    async def test_combined_files_are_bounded_before_guard_or_device_call(self):
        service, operation, invoke = self.operation_catalog("LoadCertificates")
        parameters = {
            "Certificate": [
                {
                    "CertificateID": str(index),
                    "Certificate": "data:application/octet-stream;base64,"
                    + base64.b64encode(b"certificate").decode(),
                }
                for index in range(2)
            ]
        }
        guard = Mock()
        with patch("frigate.util.onvif_schema.MAX_BINARY_BYTES", 20):
            with self.assertRaisesRegex(ValueError, "combined"):
                await invoke_bound_operation(
                    service, operation, parameters, "LoadCertificates", guard
                )
        guard.assert_not_called()
        invoke.assert_not_awaited()

    def test_firmware_restore_and_other_services_keep_their_managed_file_workflows(
        self,
    ):
        for name, service_name in (
            ("UpgradeSystemFirmware", "devicemgmt"),
            ("RestoreSystem", "devicemgmt"),
            ("LoadCertificates", "media2"),
        ):
            with self.subTest(operation=name, service=service_name):
                _, operation, _ = self.operation_catalog(name, service_name)
                self.assertEqual(operation["readiness"], "file_transfer_required")

    def test_integer_schema_ranges_reject_invalid_values_before_transport(self):
        for xsd_type, valid, invalid in (
            (xsd.UnsignedByte(), 255, (-1, 256)),
            (xsd.UnsignedShort(), 65535, (-1, 65536)),
            (xsd.Byte(), -128, (-129, 128)),
            (xsd.PositiveInteger(), 1, (0, -1)),
            (xsd.NonNegativeInteger(), 0, (-1,)),
        ):
            schema = schema_for_type(xsd_type)
            self.assertEqual(validate_parameters(schema, valid), valid)
            for value in invalid:
                with self.subTest(type=xsd_type, value=value):
                    with self.assertRaises(ValueError):
                        validate_parameters(schema, value)


class TestRealMedia2Transport(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.reply = "GetProfiles"
        self.fail = False

        def handle(request):
            self.requests.append(request)
            if self.fail:
                raise httpx.ReadTimeout(
                    "synthetic acknowledgement lost", request=request
                )
            if self.reply == "unsupported":
                xml = """<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
                  xmlns:ter="http://www.onvif.org/ver10/error"><s:Body><s:Fault>
                  <s:Code><s:Value>s:Sender</s:Value><s:Subcode><s:Value>ter:ActionNotSupported</s:Value></s:Subcode></s:Code>
                  <s:Reason><s:Text xml:lang="en">Action not supported</s:Text></s:Reason>
                  </s:Fault></s:Body></s:Envelope>"""
                return httpx.Response(
                    500, text=xml, headers={"Content-Type": "application/soap+xml"}
                )
            xml = f"""<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
              xmlns:tr2="http://www.onvif.org/ver20/media/wsdl"><s:Body>
              <tr2:{self.reply}Response/></s:Body></s:Envelope>"""
            return httpx.Response(
                200, text=xml, headers={"Content-Type": "application/soap+xml"}
            )

        self.transport = OfflineTransport(
            client=httpx.AsyncClient(transport=httpx.MockTransport(handle))
        )
        client = AsyncClient(
            str(
                SCHEMA_ROOT
                / "www.onvif.org"
                / "ver20"
                / "media"
                / "wsdl"
                / "media.wsdl"
            ),
            transport=self.transport,
        )
        self.service = SchemaService(
            client, self.transport, "http://camera.invalid/onvif/media2", MEDIA2_BINDING
        )
        self.operations = {
            item["name"]: item
            for item in describe_operations(
                self.service,
                {"GetProfiles"},
                set(),
                set(),
                set(),
                "media2",
            )
        }
        self.guard = Mock()

    async def asyncTearDown(self):
        await self.service.close()

    async def test_explicit_read_uses_real_soap_binding_once(self):
        result = await invoke_bound_operation(
            self.service,
            self.operations["GetProfiles"],
            {"Type": ["All"]},
            "",
            self.guard,
        )
        self.assertEqual(result, {"state": "read", "result": []})
        self.assertEqual(len(self.requests), 1)
        self.assertIn(b"<ns0:GetProfiles", self.requests[0].content)
        self.assertNotIn(b"Password", self.requests[0].content)
        self.guard.assert_not_called()

    async def test_mutation_requires_typed_confirmation_before_any_soap(self):
        for typed in ("", "deleteprofile", "different"):
            with self.assertRaises(ValueError):
                await invoke_bound_operation(
                    self.service,
                    self.operations["DeleteProfile"],
                    {"Token": "reviewed"},
                    typed,
                    self.guard,
                )
        self.assertEqual(self.requests, [])
        self.guard.assert_not_called()

    async def test_write_is_single_acknowledged_but_not_falsely_verified(self):
        self.reply = "DeleteProfile"
        with self.assertNoLogs("zeep.transports", level="DEBUG"):
            result = await invoke_bound_operation(
                self.service,
                self.operations["DeleteProfile"],
                {"Token": "reviewed"},
                "DeleteProfile",
                self.guard,
            )
        self.assertEqual(result["state"], "acknowledged_unverified")
        self.assertEqual(len(self.requests), 1)
        self.guard.assert_called_once()

    async def test_lost_acknowledgement_is_not_retried(self):
        self.fail = True
        with self.assertRaises(httpx.ReadTimeout):
            await invoke_bound_operation(
                self.service,
                self.operations["DeleteProfile"],
                {"Token": "reviewed"},
                "DeleteProfile",
                self.guard,
            )
        self.assertEqual(len(self.requests), 1)

    async def test_explicit_unsupported_fault_is_not_confused_with_a_missing_binding(
        self,
    ):
        self.reply = "unsupported"
        result = await invoke_bound_operation(
            self.service,
            self.operations["GetProfiles"],
            {},
            "",
            self.guard,
        )
        self.assertEqual(result, {"state": "unsupported", "result": None})
        self.assertEqual(len(self.requests), 1)

    async def test_invalid_and_extra_fields_never_reach_transport(self):
        with self.assertRaises(ValueError):
            await invoke_bound_operation(
                self.service,
                self.operations["DeleteProfile"],
                {"Token": "reviewed", "SOAPEndpoint": "http://other.invalid"},
                "DeleteProfile",
                self.guard,
            )
        self.assertEqual(self.requests, [])

    async def test_upload_uri_operation_is_not_faked_as_a_generic_json_success(self):
        with self.assertRaises(ValueError):
            await invoke_bound_operation(
                self.service,
                self.operations["AddAudioClip"],
                {},
                "AddAudioClip",
                self.guard,
            )
        self.assertEqual(self.requests, [])
        self.guard.assert_not_called()

    async def test_official_factory_adds_addressing_and_owns_a_quiet_client(self):
        calls = []

        def handle(request):
            calls.append(request)
            return httpx.Response(
                200,
                text="""<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
              xmlns:tr2="http://www.onvif.org/ver20/media/wsdl"><s:Body><tr2:GetProfilesResponse/></s:Body></s:Envelope>""",
                headers={"Content-Type": "application/soap+xml"},
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        connection = SimpleNamespace(
            username="fixture",
            password=SimpleNamespace(get_secret_value=lambda: "fixture-password"),
            auth_type="basic",
            tls_insecure=False,
        )
        with patch("frigate.ptz.wsdl_service.httpx.AsyncClient", return_value=client):
            service = await media2_service(
                "http://camera.invalid/onvif/media2",
                connection,
                UsernameToken("fixture", "fixture-password", use_digest=True),
            )
        try:
            with self.assertNoLogs("zeep.transports", level="DEBUG"):
                self.assertEqual(await service.GetProfiles(), [])
            self.assertEqual(len(calls), 1)
            content = calls[0].content
            self.assertIn(b"http://www.w3.org/2005/08/addressing", content)
            self.assertIn(b"http://www.onvif.org/ver20/media/wsdl/GetProfiles", content)
            self.assertIn(b"http://camera.invalid/onvif/media2", content)
            self.assertNotIn(b"fixture-password", content)
        finally:
            await service.close()
        self.assertTrue(client.is_closed)

    async def test_sdk_created_transport_debug_records_cannot_expose_request_xml(self):
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    content=b"<Password>response-secret</Password>",
                )
            )
        )
        transport = AsyncTransport(client=client)
        try:
            with self.assertLogs("zeep.transports", level="DEBUG") as logs:
                await transport.post(
                    "http://camera.invalid/onvif/device",
                    b"<Password>request-secret</Password>",
                    {},
                )
            text = "\n".join(logs.output)
            self.assertNotIn("request-secret", text)
            self.assertNotIn("response-secret", text)
            self.assertIn("details redacted", text)
        finally:
            await transport.aclose()
            await asyncio.to_thread(transport.wsdl_client.close)


class TestSchemaSafety(unittest.TestCase):
    def test_standard_duration_decimal_and_qname_results_remain_usable_without_xml(
        self,
    ):
        self.assertEqual(safe_result(timedelta(seconds=60)), "PT1M")
        self.assertEqual(safe_result(Decimal("0.125")), "0.125")
        self.assertEqual(
            safe_result(
                etree.QName("http://www.onvif.org/ver10/schema", "VideoSource")
            ),
            "{http://www.onvif.org/ver10/schema}VideoSource",
        )
        self.assertEqual(
            safe_result(float("nan")), {"redacted": True, "kind": "non_finite"}
        )

    def test_results_never_return_passwords_private_keys_or_uri_credentials(self):
        result = safe_result(
            {
                "Password": "sentinel-password",
                "PrivateKey": b"sentinel-key",
                "Uri": "rtsp://user:sentinel@camera.invalid/path?auth=sentinel-token",
                "Xml": SimpleNamespace(),
            }
        )
        text = str(result)
        self.assertNotIn("sentinel", text)
        self.assertEqual(result["Uri"], "rtsp://camera.invalid")

    def test_rejects_extra_fields_and_boolean_numeric_coercion(self):
        schema = {"type": "object", "properties": {"Port": {"type": "integer"}}}
        for parameters in (
            {"Port": True},
            {"Port": 1.5},
            {"Endpoint": "http://other.invalid"},
        ):
            with self.assertRaises(ValueError):
                validate_parameters(schema, parameters)

    def test_nested_passwords_and_private_keys_are_write_only_form_fields(self):
        user = xsd.ComplexType(
            [
                xsd.Element("Username", xsd.String()),
                xsd.Element("Password", xsd.String()),
                xsd.Element("PrivateKey", xsd.Base64Binary(), min_occurs=0),
            ]
        )
        schema = schema_for_type(user)
        self.assertTrue(schema["properties"]["Password"]["writeOnly"])
        self.assertEqual(schema["properties"]["Password"]["format"], "password")
        self.assertEqual(schema["properties"]["PrivateKey"]["format"], "data-url")
        self.assertNotIn("default", schema["properties"]["Password"])

    def test_required_wildcard_xml_is_not_invented_or_silently_accepted(self):
        schema = schema_for_type(
            xsd.ComplexType([xsd.Any(min_occurs=1, max_occurs="unbounded")])
        )
        self.assertFalse(schema_ready(schema))
        with self.assertRaises(ValueError):
            validate_parameters(schema, {})

    def test_service_endpoints_are_saved_camera_only_without_auth_or_tls_downgrade(
        self,
    ):
        self.assertEqual(
            pinned_service_url(
                "http://camera.invalid:8000/onvif/media", "camera.invalid"
            ),
            "http://camera.invalid:8000/onvif/media",
        )
        self.assertEqual(
            pinned_service_url("http://[2001:db8::1]/onvif/media", "2001:db8::1"),
            "http://[2001:db8::1]/onvif/media",
        )
        for url, host in (
            ("http://other.invalid/soap", "camera.invalid"),
            ("http://user:private@camera.invalid/soap", "camera.invalid"),
            ("http://camera.invalid/soap?token=private", "camera.invalid"),
            ("http://camera.invalid/soap", "https://camera.invalid"),
        ):
            with self.assertRaises(ValueError):
                pinned_service_url(url, host)

    def test_subscription_query_opt_in_preserves_camera_and_tls_boundaries(self):
        address = "http://camera.invalid:8000/onvif/Subscription?Idx=12"
        self.assertEqual(
            pinned_service_url(address, "camera.invalid", allow_query=True),
            address,
        )
        with self.assertRaises(ValueError):
            pinned_service_url(address, "camera.invalid")
        for address, host in (
            ("http://other.invalid/onvif/Subscription?Idx=12", "camera.invalid"),
            ("http://user@camera.invalid/onvif/Subscription?Idx=12", "camera.invalid"),
            (
                "http://camera.invalid/onvif/Subscription?Idx=12#fragment",
                "camera.invalid",
            ),
            (
                "http://camera.invalid/onvif/Subscription?Idx=12",
                "https://camera.invalid",
            ),
            (
                "http://camera.invalid/onvif/Subscription?Idx=" + "1" * 1025,
                "camera.invalid",
            ),
        ):
            with self.subTest(address=address):
                with self.assertRaises(ValueError):
                    pinned_service_url(address, host, allow_query=True)
