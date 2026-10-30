"""Use synthetic signing keys only; full native API imports require Linux."""

import hashlib
import os
import unittest
from types import SimpleNamespace


@unittest.skipIf(os.name == "nt", "Full API imports require Linux")
class NativeSigningGenerationTests(unittest.TestCase):
    def setUp(self):
        from joserfc.jwk import OctKey

        from frigate.api.native import _signing_generation

        self.oct_key = OctKey
        self.generation = _signing_generation
        self.secret = b"synthetic-native-signing-regression-key-32"

    def test_initialized_oct_key_uses_raw_bytes_without_changing_app_key(self):
        key = self.oct_key.import_key(self.secret)
        app = SimpleNamespace(jwt_token=key)
        self.assertEqual(
            self.generation(app), hashlib.sha256(self.secret).hexdigest()
        )
        self.assertIs(app.jwt_token, key)

    def test_byte_string_and_oct_key_representations_have_the_same_generation(self):
        expected = hashlib.sha256(self.secret).hexdigest()
        for key in (
            self.secret,
            bytearray(self.secret),
            self.secret.decode(),
            self.oct_key.import_key(self.secret),
        ):
            with self.subTest(key_type=type(key).__name__):
                self.assertEqual(
                    self.generation(SimpleNamespace(jwt_token=key)), expected
                )

    def test_oct_key_rotation_changes_generation(self):
        first = self.oct_key.import_key(self.secret)
        second = self.oct_key.import_key(self.secret + b"-rotated")
        self.assertNotEqual(
            self.generation(SimpleNamespace(jwt_token=first)),
            self.generation(SimpleNamespace(jwt_token=second)),
        )

    def test_missing_and_unsupported_keys_fail_closed(self):
        for key in (None, object(), 123, True):
            with self.subTest(key_type=type(key).__name__):
                with self.assertRaisesRegex(ValueError, "signing key is unavailable"):
                    self.generation(SimpleNamespace(jwt_token=key))

    def test_empty_keys_fail_closed(self):
        for key in ("", b"", bytearray()):
            with self.subTest(key_type=type(key).__name__):
                with self.assertRaisesRegex(ValueError, "signing key is unavailable"):
                    self.generation(SimpleNamespace(jwt_token=key))
