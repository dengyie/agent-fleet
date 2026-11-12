import hashlib
import unittest

from hub.domain.workflow_identity import canonical_digest, dispatch_token


class WorkflowIdentityTests(unittest.TestCase):
    def test_digest_has_fixed_utf8_representation(self):
        expected = hashlib.sha256('{"a":"中","b":2}'.encode("utf-8")).hexdigest()
        self.assertEqual(canonical_digest({"b": 2, "a": "中"}), expected)
        self.assertEqual(canonical_digest({"a": "中", "b": 2}), expected)

    def test_dispatch_has_fixed_vector_and_generation_boundary(self) -> None:
        first = dispatch_token("owner-a", "wf-a", "develop", 1)
        self.assertEqual(
            first,
            "a1d22a0e30bac2e280aaaf9d1951bee06c971148966ac02f2c7febe6f0f2e9e5",
        )
        self.assertNotEqual(first, dispatch_token("owner-a", "wf-a", "develop", 2))
        self.assertNotEqual(first, dispatch_token("owner-b", "wf-a", "develop", 1))

    def test_generation_is_an_exact_positive_integer(self) -> None:
        for value in (True, False, 0, -1, 1.0, "1", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                dispatch_token("owner-a", "wf-a", "develop", value)

    def test_identifiers_cannot_alias_nul_delimited_parts(self) -> None:
        for value in ("", "owner\x00other", "x" * 129, "\ud800", None):
            with self.subTest(value=repr(value)), self.assertRaises(ValueError):
                dispatch_token(value, "wf-a", "develop", 1)

    def test_non_json_values_and_invalid_unicode_are_rejected(self) -> None:
        for value in (float("nan"), float("inf"), {1: "x"}, {"x": {1}}, "\ud800"):
            with self.subTest(value=repr(value)), self.assertRaises(ValueError):
                canonical_digest(value)

    def test_digest_has_byte_and_depth_limits(self) -> None:
        with self.assertRaises(ValueError):
            canonical_digest("x" * 65536)
        value = None
        for _ in range(34):
            value = [value]
        with self.assertRaises(ValueError):
            canonical_digest(value)


if __name__ == "__main__":
    unittest.main()
