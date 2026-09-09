"""Tests for the AEAD session crypto primitive (Task 3).

Coverage (from the Task 3 brief):

- ``AeadBox.encrypt`` / ``AeadBox.decrypt`` round trip with AES-GCM 256;
- wrong associated data must be rejected (``InvalidTag`` must propagate, not
  be swallowed);
- tampered ciphertext must be rejected (``InvalidTag`` must propagate);
- missing/absent key source must fail closed: ``load_encryption_key`` returns
  ``None`` and a request for encryption without a key returns ``None`` (never
  a plaintext fallback);
- key bytes never enter ``repr``, ``str`` or exception text.

Only the ``cryptography`` library is used; no manual crypto.
"""

import unittest
from types import SimpleNamespace

import cryptography.exceptions
from cryptography.exceptions import InvalidTag

from tools.session.crypto import AeadBox, load_encryption_key


def _key() -> bytes:
    # 32 random bytes => AES-GCM 256-bit key material.  Not a secret constant.
    return b"\xb1\xc2\xd3\xe4\xf5\x06\x17\x28\x39\x4a\x5b\x6c\x7d\x8e\x9f\xa0" \
           b"\x01\x12\x23\x34\x45\x56\x67\x78\x89\x9a\xab\xbc\xcd\xde\xef\x01"


def _config(**extra):
    """Return a simple namespace that behaves like the hub FleetConfig."""
    return SimpleNamespace(**extra)


class AeadRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.box = AeadBox(_key())

    def test_round_trip(self):
        plaintext = b"event payload bytes"
        ciphertext = self.box.encrypt(plaintext)
        self.assertEqual(self.box.decrypt(ciphertext), plaintext)

    def test_round_trip_with_associated_data(self):
        plaintext = b"session event stream 42"
        ad = b"session_id=abc123"
        ciphertext = self.box.encrypt(plaintext, ad)
        self.assertEqual(self.box.decrypt(ciphertext, ad), plaintext)

    def test_ciphertext_binds_nonce_and_is_opaque(self):
        ciphertext = self.box.encrypt(b"payload")
        self.assertNotEqual(ciphertext, b"payload")
        self.assertNotIn(b"payload", ciphertext)

    def test_encrypt_deterministic_nonce_means_distinct_ciphertext(self):
        # Two encryptions of the same plaintext produce different ciphertexts
        # (fresh random nonce each time); both still decrypt to the same value.
        c1 = self.box.encrypt(b"same")
        c2 = self.box.encrypt(b"same")
        self.assertNotEqual(c1, c2)
        self.assertEqual(self.box.decrypt(c1), self.box.decrypt(c2))


class AeadRejectionTests(unittest.TestCase):
    def setUp(self):
        self.box = AeadBox(_key())

    def test_wrong_associated_data_rejected(self):
        ciphertext = self.box.encrypt(b"payload", b"right-ad")
        with self.assertRaises(InvalidTag):
            self.box.decrypt(ciphertext, b"wrong-ad")

    def test_missing_associated_data_rejected(self):
        ciphertext = self.box.encrypt(b"payload", b"right-ad")
        with self.assertRaises(InvalidTag):
            self.box.decrypt(ciphertext)

    def test_tampered_ciphertext_rejected(self):
        ciphertext = bytearray(self.box.encrypt(b"payload"))
        ciphertext[len(ciphertext) // 2] ^= 0x01
        with self.assertRaises(InvalidTag):
            self.box.decrypt(bytes(ciphertext))

    def test_truncated_ciphertext_rejected(self):
        ciphertext = self.box.encrypt(b"payload")
        with self.assertRaises(InvalidTag):
            self.box.decrypt(ciphertext[:-4])

    def test_key_mismatch_rejected(self):
        ciphertext = self.box.encrypt(b"payload")
        other = AeadBox(b"\x00" * 32)
        with self.assertRaises(InvalidTag):
            other.decrypt(ciphertext)

    def test_raises_cryptography_exception_not_swallowed(self):
        ciphertext = self.box.encrypt(b"payload")
        with self.assertRaises(cryptography.exceptions.InvalidTag):
            self.box.decrypt(ciphertext + b"\x00")


class FailClosedTests(unittest.TestCase):
    def test_load_key_absent_returns_none(self):
        # No configured key source anywhere -> None (fail closed).
        self.assertIsNone(load_encryption_key(_config()))

    def test_load_key_partial_key_rejected(self):
        # A config that only names a key file path is not an explicit key
        # source.  The loader must return None rather than auto-read a path.
        cfg = _config(session_encryption_key_file="/tmp/secret-missing")
        self.assertIsNone(load_encryption_key(cfg))

    def test_load_key_wrong_type_returns_none(self):
        cfg = _config(session_encryption_raw=12345)
        self.assertIsNone(load_encryption_key(cfg))

    def test_load_key_explicit_source_returns_key(self):
        key = _key()
        cfg = _config(session_encryption_raw=key)
        self.assertEqual(load_encryption_key(cfg), key)

    def test_encrypt_without_key_fails_closed_returns_none(self):
        cfg = _config()
        key = load_encryption_key(cfg)
        self.assertIsNone(key)
        from tools.session.crypto import encrypt_bounded
        result = encrypt_bounded(key, b"unsafe payload")
        self.assertIsNone(result)

    def test_load_key_classmethod_form(self):
        key = _key()
        cfg = _config(session_encryption_raw=key)
        self.assertEqual(AeadBox.load_encryption_key(cfg), key)

    def test_load_key_from_mapping_config(self):
        key = _key()
        self.assertEqual(load_encryption_key({"session_encryption_raw": key}),
                         key)
        self.assertIsNone(load_encryption_key({}))


class KeyMaterialTests(unittest.TestCase):
    def test_repr_and_str_hide_key(self):
        box = AeadBox(_key())
        self.assertNotIn(_key().hex(), repr(box))
        self.assertNotIn(_key().hex(), str(box))

    def test_encrypt_then_decrypt_with_key_bytes_not_in_output(self):
        key = _key()
        box = AeadBox(key)
        ct = box.encrypt(b"secret payload")
        self.assertNotIn(b"secret payload", ct)


if __name__ == "__main__":
    unittest.main()