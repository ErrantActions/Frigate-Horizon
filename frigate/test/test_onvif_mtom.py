"""Real multipart byte roundtrips with synthetic SOAP and camera backup data."""

import unittest

from zeep.exceptions import Fault

from frigate.util.onvif_mtom import (
    backup_response_files,
    encode_mtom,
    parse_soap_response,
)


def envelope(operation, fields=""):
    """Construct only synthetic protocol fixtures, never device traffic."""
    return (
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:tds="http://www.onvif.org/ver10/device/wsdl" '
        'xmlns:tt="http://www.onvif.org/ver10/schema" '
        'xmlns:xop="http://www.w3.org/2004/08/xop/include">'
        f"<s:Body><tds:{operation}Response>{fields}</tds:{operation}Response>"
        "</s:Body></s:Envelope>"
    ).encode()


class TestOnvifMtom(unittest.TestCase):
    def test_backup_attachment_roundtrip_preserves_all_binary_bytes(self):
        original = b"\x00\xff\r\nbinary\npayload\rend"
        content, content_type = encode_mtom(
            envelope(
                "GetSystemBackup",
                "<tds:BackupFiles><tt:Name>config</tt:Name><tt:Data>"
                '<xop:Include href="cid:backup-one"/></tt:Data></tds:BackupFiles>',
            ),
            {"backup-one": original},
        )
        response, attachments = parse_soap_response(
            content, content_type, "GetSystemBackup"
        )
        files = backup_response_files(response, attachments)
        self.assertEqual(files, [{"Name": "config", "Data": {"Data": original}}])

    def test_foreign_or_missing_attachment_reference_is_rejected(self):
        for reference in ("https://other.invalid/private", "cid:missing"):
            response, attachments = parse_soap_response(
                envelope(
                    "GetSystemBackup",
                    "<tds:BackupFiles><tt:Name>config</tt:Name><tt:Data>"
                    f'<xop:Include href="{reference}"/></tt:Data></tds:BackupFiles>',
                ),
                "application/soap+xml",
                "GetSystemBackup",
            )
            with self.assertRaises(ValueError):
                backup_response_files(response, attachments)

    def test_wrong_operation_or_document_type_is_not_a_successful_restore(self):
        for content in (
            envelope("GetSystemBackup"),
            b'<!DOCTYPE test [<!ENTITY value "fixture">]>' + envelope("RestoreSystem"),
            b"not XML",
        ):
            with self.assertRaises(ValueError):
                parse_soap_response(content, "application/soap+xml", "RestoreSystem")

    def test_fault_does_not_reflect_vendor_private_error_text(self):
        content = (
            b'<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
            b"<s:Body><s:Fault>private-sentinel</s:Fault></s:Body></s:Envelope>"
        )
        with self.assertRaises(Fault) as error:
            parse_soap_response(content, "application/soap+xml", "RestoreSystem")
        self.assertNotIn("private-sentinel", str(error.exception))

    def test_header_injection_and_invalid_server_content_ids_are_rejected(self):
        with self.assertRaises(ValueError):
            parse_soap_response(
                envelope("RestoreSystem"), "x\r\nInjected: true", "RestoreSystem"
            )
        with self.assertRaises(ValueError):
            encode_mtom(envelope("RestoreSystem"), {"bad\r\nid": b"fixture"})
