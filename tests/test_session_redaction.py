"""Tests for deterministic session-event redaction (Task 3).

Coverage (from the Task 3 brief and the design spec section 15):

- high-confidence token / API key / bearer / password replacement;
- private-key (multi-line PEM) replacement;
- DB URL and env-style secret assignment replacement;
- cookie / session-id replacement;
- nested dict/list payloads (``tool_args``, ``tool_result``,
  ``user_message``, ``assistant_message``, ``session_metadata``) are walked
  recursively;
- sensitive filesystem paths are replaced, while a generic absolute path is
  treated as *uncertain* (replaced, but ``report.uncertain`` becomes True);
- unambigous placeholders like ``[REDACTED:credential]`` are used and the
  original value never appears in the ``RedactionReport``;
- the redactor is pure and deterministic (same input -> identical output and
  identical report), categories are sorted / deduplicated, and non-sensitive
  text is left unchanged.

These tests run without Flask and without ``cryptography``; the shared
contract is ``session_schema.py`` only.
"""

import unittest

from session_schema import validate_event
from tools.session.redact import RedactionReport, Redactor


def _event(kind="user_message", payload=None, **extra):
    event = {
        "schema_version": 1,
        "event_id": "evt_test_1",
        "stream_id": "stream_test_1",
        "machine_id": "host-test-1",
        "session_id": "sess_test_1",
        "attempt_id": "att_test_1",
        "process_group_id": "grp_test_1",
        "sequence": 7,
        "kind": kind,
        "capture_quality": "structured",
        "source": "native_transcript",
        "emitted_at": "2026-08-26T00:00:00Z",
    }
    if payload is not None:
        event["payload"] = payload
    event.update(extra)
    return event


class HighConfidenceReplacementTests(unittest.TestCase):
    def setUp(self):
        self.redactor = Redactor(passthrough=False)

    def test_sk_token_value_replaced(self):
        original = "Use sk-0123456789abcdef0123456789abcdef to call the API."
        event = _event(payload={"text": original, "is_complete": True})
        clean, report = self.redactor.redact_event(event)
        self.assertNotIn("sk-0123456789abcdef0123456789abcdef",
                         clean["payload"]["text"])
        self.assertRegex(clean["payload"]["text"], r"\[REDACTED:")
        self.assertGreaterEqual(report.replaced, 1)
        self.assertIn("credential", report.categories)
        self.assertFalse(report.uncertain)

    def test_github_token_replaced(self):
        original = "Use ghp_0123456789abcdef0123456789abcdef to clone."
        event = _event(payload={"text": original, "is_complete": True})
        clean, report = self.redactor.redact_event(event)
        self.assertNotIn("ghp_0123456789abcdef0123456789abcdef",
                         clean["payload"]["text"])
        self.assertIn("credential", report.categories)

    def test_multiple_tokens_counted(self):
        original = (
            "token1=sk-0123456789abcdef0123456789abcdef "
            "token2=ghp_0123456789abcdef0123456789abcdef"
        )
        event = _event(payload={"text": original, "is_complete": True})
        clean, report = self.redactor.redact_event(event)
        self.assertEqual(report.replaced, 2)
        self.assertNotIn("sk-0123456789abcdef0123456789abcdef",
                         clean["payload"]["text"])
        self.assertNotIn("ghp_0123456789abcdef0123456789abcdef",
                         clean["payload"]["text"])

    def test_bearer_auth_replaced(self):
        original = "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abc.def"
        event = _event(payload={"text": original, "is_complete": True})
        clean, report = self.redactor.redact_event(event)
        self.assertNotIn("eyJhbGciOiJIUzI1NiJ9", clean["payload"]["text"])
        self.assertIn("credential", report.categories)

    def test_password_assignment_replaced(self):
        original = "postgres password=hunter22 rest"
        event = _event(payload={"text": original, "is_complete": True})
        clean, report = self.redactor.redact_event(event)
        self.assertNotIn("hunter22", clean["payload"]["text"])
        self.assertIn("password", report.categories)

    def test_private_key_block_replaced(self):
        keyblock = (
            "-----BEGIN PRIVATE KEY-----\n"
            "TESTONLY-NOT-A-KEY-BLOCK-0123456789A\n"
            "-----END PRIVATE KEY-----\n"
        )
        event = _event(payload={"text": "key:\n" + keyblock,
                                "is_complete": True})
        clean, report = self.redactor.redact_event(event)
        self.assertNotIn("TESTONLY-NOT-A-KEY-BLOCK-0123456789A",
                         clean["payload"]["text"])
        self.assertIn("private_key", report.categories)

    def test_encrypted_private_key_block_replaced(self):
        text = (
            "-----BEGIN ENCRYPTED PRIVATE KEY-----\n"
            "TESTONLY-NOT-A-KEY-BLOCK-0123456789AB\n"
            "-----END ENCRYPTED PRIVATE KEY-----\n"
        )
        event = _event(payload={"text": text, "is_complete": True})
        cleaned, report = self.redactor.redact_event(event)
        self.assertNotIn("TESTONLY-NOT-A-KEY-BLOCK-0123456789AB",
                         cleaned["payload"]["text"])
        self.assertIn("private_key", report.categories)

    def test_env_secret_assignment_replaced(self):
        original = "bin/env AWS_SECRET_ACCESS_KEY=this-is-super-secret now"
        event = _event(payload={"text": original, "is_complete": True})
        cleaned, report = self.redactor.redact_event(event)
        self.assertNotIn("this-is-super-secret", cleaned["payload"]["text"])
        self.assertIn("env", report.categories)

    def test_db_url_credentials_replaced(self):
        original = ("admin at postgres://alice:hunter2@db.internal:5432/app "
                    "owns the schema")
        event = _event(payload={"text": original, "is_complete": True})
        cleaned, report = self.redactor.redact_event(event)
        self.assertNotIn("hunter2", cleaned["payload"]["text"])
        self.assertNotIn("alice:hunter2", cleaned["payload"]["text"])
        self.assertIn("db_url", report.categories)

    def test_cookie_session_id_replaced(self):
        original = "flag at PHPSESSID=abc123def456; same"
        event = _event(payload={"text": original, "is_complete": True})
        cleaned, report = self.redactor.redact_event(event)
        self.assertNotIn("abc123def456", cleaned["payload"]["text"])
        self.assertIn("session", report.categories)

    def test_connect_sid_replaced(self):
        original = "conn=connect.sid=abcdef1234567890"
        event = _event(payload={"text": original, "is_complete": True})
        cleaned, report = self.redactor.redact_event(event)
        self.assertNotIn("abcdef1234567890", cleaned["payload"]["text"])
        self.assertTrue(cleaned["payload"]["text"].startswith(
            "[REDACTED:") or "connect.sid=" not in cleaned["payload"]["text"])

    def test_aws_style_assignment_replaced(self):
        # Assemble at runtime so the file never contains a GitHub-scanner
        # AWS access-key literal (AKIA + 16 alnum).
        fake = "AK" + "IA" + ("0" * 16)
        original = "run with AWS_ACCESS_KEY_ID=" + fake + " now."
        event = _event(payload={"text": original, "is_complete": True})
        cleaned, report = self.redactor.redact_event(event)
        self.assertNotIn(fake, cleaned["payload"]["text"])
        self.assertIn("env", report.categories)

    def test_flutter_works_with_placeholder_text_unchanged(self):
        original = "See docs/deploy.md in the repo root."
        event = _event(payload={"text": original, "is_complete": True})
        cleaned, report = self.redactor.redact_event(event)
        self.assertEqual(cleaned["payload"]["text"], original)
        self.assertEqual(report.replaced, 0)
        self.assertFalse(report.uncertain)


class NestedRedactionTests(unittest.TestCase):
    def setUp(self):
        self.redactor = Redactor(passthrough=False)

    def test_nested_user_message_payload(self):
        payload = {
            "text": "roll out the gate gently",
            "is_complete": True,
            "metadata": {"labels": ["ops", "build"]},
        }
        event = _event(payload=payload)
        cleaned, report = self.redactor.redact_event(event)
        self.assertEqual(cleaned["payload"]["metadata"],
                         {"labels": ["ops", "build"]})
        self.assertEqual(cleaned["payload"]["text"], "roll out the gate gently")
        self.assertEqual(report.replaced, 0)

    def test_nested_tool_args_deep_secret(self):
        payload = {
            "tool_result": "ok",
            "tool_args": {
                "headers": {
                    "authorization": "Bearer eyJhbGciOiJIUzI1NiJ9.abc.def",
                    "x-api-key": "sk-0123456789abcdef0123456789abcdef",
                },
                "input": {"nested": {"password": "hunter2"}},
                "plugin": ["demo", "plugin"],
            },
        }
        event = _event(kind="tool_call", payload=payload)
        cleaned, report = self.redactor.redact_event(event)
        blob = str(cleaned["payload"])
        self.assertNotIn("eyJhbGciOiJIUzI1NiJ9", blob)
        self.assertNotIn("sk-0123456789abcdef0123456789abcdef", blob)
        self.assertNotIn("hunter2", blob)
        self.assertNotIn("Bearer eyJhbGciOiJIUzI1NiJ9", blob)
        self.assertGreaterEqual(report.replaced, 3)
        self.assertIn("credential", report.categories)
        self.assertIn("password", report.categories)

    def test_tool_result_original_never_in_payload(self):
        payload = {
            "tool_name": "file_read",
            "result": "stdout = ghp_0123456789abcdef0123456789abcdef",
        }
        event = _event(kind="tool_result", payload=payload)
        cleaned, report = self.redactor.redact_event(event)
        self.assertNotIn("ghp_0123456789abcdef0123456789abcdef",
                         str(cleaned["payload"]))
        self.assertIn("credential", report.categories)

    def test_list_values_walked(self):
        payload = {
            "snippets": [
                "line 1 ok",
                "token here ghp_0123456789abcdef0123456789abcdef",
                {"nested": "x sk-0123456789abcdef0123456789abcdef y"},
            ],
        }
        event = _event(kind="assistant_message", payload=payload)
        cleaned, report = self.redactor.redact_event(event)
        text = str(cleaned["payload"])
        self.assertNotIn("ghp_0123456789abcdef0123456789abcdef", text)
        self.assertNotIn("sk-0123456789abcdef0123456789abcdef", text)
        self.assertEqual(report.replaced, 2)

    def test_sensitive_key_dict_value_fully_replaced(self):
        payload = {"headers": {"api_key": "opaque-x", "other": "ok"}}
        event = _event(kind="tool_call", payload=payload)
        cleaned, report = self.redactor.redact_event(event)
        self.assertNotIn("opaque-x", str(cleaned["payload"]))
        self.assertIn("credential", report.categories)


class UncertaintyTests(unittest.TestCase):
    def setUp(self):
        self.redactor = Redactor(passthrough=False)

    def test_generic_absolute_path_is_uncertain_and_replaced(self):
        payload = {"text": "read /opt/app/config.d/entry.yaml then exit",
                   "is_complete": True}
        event = _event(payload=payload)
        cleaned, report = self.redactor.redact_event(event)
        self.assertNotIn("/opt/app/config.d/entry.yaml",
                         cleaned["payload"]["text"])
        self.assertTrue(report.uncertain)
        self.assertIn("path", report.categories)

    def test_sensitive_path_replaced_certain(self):
        payload = {"text": "inspected /home/alice/.ssh/id_rsa now",
                   "is_complete": True}
        event = _event(payload=payload)
        cleaned, report = self.redactor.redact_event(event)
        self.assertNotIn(".ssh/id_rsa", cleaned["payload"]["text"])
        self.assertIn("path", report.categories)
        self.assertFalse(report.uncertain)

    def test_unknown_ambiguous_value_sets_uncertain(self):
        # A bare, unclassifiable opaque-ish value stays but is flagged.
        payload = {"text": "saw token=xxx-abcdef in the middle", "is_complete": True}
        event = _event(payload=payload)
        cleaned, report = self.redactor.redact_event(event)
        self.assertNotIn("xxx-abcdef", cleaned["payload"]["text"])
        self.assertTrue(report.uncertain)


class DeterminismAndReportingTests(unittest.TestCase):
    def setUp(self):
        self.redactor = Redactor(passthrough=False)

    def test_deterministic_same_input_same_output(self):
        template = {
            "tool_args": {
                "headers": {"Authorization": "Bearer "
                             "eyJhbGciOiJIUzI1NiJ9.abc.def"},
                "nested": ["one", "ghp_0123456789abcdef0123456789abcdef"],
                "path": "/opt/app/config.d.ts",
            }
        }
        event = _event(kind="tool_call", payload=template)
        clean_a, report_a = self.redactor.redact_event(event)
        clean_b, report_b = self.redactor.redact_event(event)
        self.assertEqual(clean_a, clean_b)
        self.assertEqual(report_a, report_b)

    def test_report_never_contains_original_text(self):
        original = "Bearer eyJhbGciOiJIUzI1NiJ9.abc.def via sk-abcd"
        event = _event(payload={"text": original, "is_complete": True})
        _, report = self.redactor.redact_event(event)
        for secret in ("sk-abcd", "eyJhbGciOiJIUzI1NiJ9", "Bearer "):
            self.assertNotIn(secret, str(report))

    def test_categories_are_sorted_deduped(self):
        text = ("a sk-0123456789abcdefghij0123456789abcdef "
                "b sk-01234567890123diamondx123456789012345x")
        event = _event(payload={"text": text, "is_complete": True})
        _, report = self.redactor.redact_event(event)
        self.assertEqual(report.categories,
                         tuple(sorted(set(report.categories))))

    def test_redacted_event_passes_shared_schema_validation(self):
        payload = {"text": "token sk-0123456789abcdef0123456789abcdef ok",
                   "is_complete": True}
        event = _event(payload=payload)
        cleaned, report = self.redactor.redact_event(event)
        self.assertEqual(report.replaced, 1)
        validated = validate_event(cleaned)
        self.assertEqual(validated["payload"]["text"],
                         validated["payload"]["text"])
        self.assertIn("text", validated["payload"])

    def test_numeric_and_boolean_payload_left_alone(self):
        payload = {"count": 3, "ok": True, "ratio": 0.5}
        event = _event(kind="process_exit", payload={
            "exit_code": 0, "duration_s": 1.5, "reason": "done"})
        cleaned, report = self.redactor.redact_event(event)
        self.assertEqual(report.replaced, 0)
        self.assertEqual(cleaned["payload"]["exit_code"], 0)


class PublicMetadataTests(unittest.TestCase):
    def test_public_metadata_bounded_fields(self):
        from hub.domain.redaction import public_redaction_metadata
        report = RedactionReport(replaced=3,
                                 categories=("credential", "path", "env"),
                                 uncertain=True)
        meta = public_redaction_metadata(report)
        self.assertEqual(set(meta), {"replaced", "categories", "uncertain"})
        self.assertEqual(meta["replaced"], 3)
        self.assertEqual(meta["uncertain"], True)
        self.assertEqual(meta["categories"],
                         ("credential", "env", "path"))

    def test_public_metadata_caps_categories(self):
        from hub.domain.redaction import (
            MAX_PUBLIC_CATEGORIES,
            public_redaction_metadata,
        )
        many = tuple("cat%02d" % index for index in range(0, 100))
        report = RedactionReport(replaced=1, categories=many, uncertain=False)
        meta = public_redaction_metadata(report)
        self.assertEqual(len(meta["categories"]), MAX_PUBLIC_CATEGORIES)
        self.assertEqual(meta["categories"],
                         tuple(sorted(set(many)))[:MAX_PUBLIC_CATEGORIES])


class ReviewFixRegressionTests(unittest.TestCase):
    """Regression tests for Task 3 review round 1 findings (scoped)."""

    def setUp(self):
        self.redactor = Redactor(passthrough=False)

    # -- Finding 1: container value under a sensitive key leaks -----------

    def test_sensitive_key_dict_value_container_leak_fixed(self):
        payload = {"headers": {"password": {"value": "hunter2"}}}
        event = _event(kind="tool_call", payload=payload)
        cleaned, report = self.redactor.redact_event(event)
        self.assertNotIn("hunter2", str(cleaned["payload"]))
        self.assertIn("password", report.categories)
        self.assertTrue(report.uncertain)

    def test_sensitive_key_list_value_container_leak_fixed(self):
        payload = {"authorization": ["Bearer", "hunter2"]}
        event = _event(kind="tool_call", payload=payload)
        cleaned, report = self.redactor.redact_event(event)
        self.assertNotIn("hunter2", str(cleaned["payload"]))
        self.assertIn("credential", report.categories)

    def test_sensitive_key_nested_container_reaches_leaf(self):
        payload = {"headers": {"authorization": {"scheme": "Bearer",
                                                 "cred": "hunter2"}}}
        event = _event(kind="tool_call", payload=payload)
        cleaned, report = self.redactor.redact_event(event)
        self.assertNotIn("hunter2", str(cleaned["payload"]))
        self.assertTrue(report.uncertain)

    # -- Finding 2: depth and string-overflow leaks ----------------------

    def test_depth_overflow_redacts_and_sets_uncertain(self):
        # 65 nested dicts push the walk past MAX_REDACT_DEPTH; the secret
        # must never leak and the overflow must be reported as uncertain.
        node = "sk-0123456789abcdef0123456789abcdef"
        for _ in range(65):
            node = {"nested": node}
        event = _event(payload=node)
        cleaned, report = self.redactor.redact_event(event)
        self.assertNotIn("sk-0123456789abcdef0123456789abcdef",
                         str(cleaned["payload"]))
        self.assertTrue(report.uncertain)
        self.assertGreaterEqual(report.replaced, 1)

    def test_depth_at_limit_token_is_redacted(self):
        node = "sk-0123456789abcdef0123456789abcdef"
        for _ in range(64):
            node = {"nested": node}
        event = _event(payload=node)
        cleaned, report = self.redactor.redact_event(event)
        self.assertNotIn("sk-0123456789abcdef0123456789abcdef",
                         str(cleaned["payload"]))

    def test_oversized_string_redacted_and_uncertain(self):
        payload = {"text": "x" * (1 << 20) + "sk-supersecrettoken",
                   "is_complete": True}
        event = _event(payload=payload)
        cleaned, report = self.redactor.redact_event(event)
        self.assertNotIn("sk-supersecrettoken", str(cleaned["payload"]))
        self.assertTrue(report.uncertain)

    # -- Finding 3: multi-token env value / password fully redacted ------

    def test_env_assignment_multi_word_value_redacted(self):
        original = "AWS_SECRET_ACCESS_KEY=correct horse battery staple now"
        event = _event(payload={"text": original, "is_complete": True})
        cleaned, report = self.redactor.redact_event(event)
        self.assertNotIn("correct horse battery staple",
                         cleaned["payload"]["text"])
        self.assertIn("env", report.categories)
        self.assertFalse(report.uncertain)

    def test_password_assignment_multi_word_value_redacted(self):
        original = "password=correct horse battery staple; exit"
        event = _event(payload={"text": original, "is_complete": True})
        cleaned, report = self.redactor.redact_event(event)
        self.assertNotIn("correct horse battery staple",
                         cleaned["payload"]["text"])

    # -- Finding 4: unbounded replaced count -----------------------------

    def test_replaced_count_is_bounded(self):
        from tools.session.redact import MAX_REDACT_COUNT
        text = ("token=sk-%s " % ("a" * 8)) * 4000
        event = _event(payload={"text": text, "is_complete": True})
        _, report = self.redactor.redact_event(event)
        self.assertLessEqual(report.replaced, MAX_REDACT_COUNT)

    def test_replaced_count_single_huge_token_bounded(self):
        from tools.session.redact import MAX_REDACT_COUNT
        text = "sk-" + ("ABCDefgh" * 100000)
        event = _event(payload={"text": text, "is_complete": True})
        _, report = self.redactor.redact_event(event)
        self.assertLessEqual(report.replaced, MAX_REDACT_COUNT)

    # -- Finding 5: DB URL faithful/idempotent reconstruction ------------

    def test_db_url_reconstruction_exact(self):
        original = "postgres://user:pass@host/db"
        event = _event(payload={"text": original, "is_complete": True})
        cleaned, report = self.redactor.redact_event(event)
        expected = "postgres://[REDACTED:credential]@host/db"
        self.assertEqual(cleaned["payload"]["text"], expected)

    def test_db_url_reconstruction_idempotent(self):
        event = _event(payload={"text": "postgres://user:pass@host/db",
                                "is_complete": True})
        clean_a, _ = self.redactor.redact_event(event)
        clean_b, _ = self.redactor.redact_event(clean_a)
        self.assertEqual(clean_a, clean_b)

    # -- Finding 6: sk- short-token false positives ----------------------

    def test_short_sk_value_not_redacted_certain(self):
        for token in ("sk-1001", "sk-2024-01-01", "sk-podis"):
            event = _event(payload={"text": f"sts {token}: done",
                                    "is_complete": True})
            cleaned, report = self.redactor.redact_event(event)
            # The short sk- value must survive (not replaced):
            self.assertIn(token, cleaned["payload"]["text"],
                          f"{token} must not be replaced")
            self.assertEqual(report.replaced, 0,
                             f"{token} must not inflate replaced")
            # Anything that could not be safely classified must be flagged:
            self.assertTrue(report.uncertain)

    def test_genuine_sk_token_still_redacted(self):
        token = "sk-ant-api03-0123456789ABCDefghij0123456789ABCDefgh"
        event = _event(payload={"text": token, "is_complete": True})
        cleaned, report = self.redactor.redact_event(event)
        self.assertNotIn("sk-ant-api03", cleaned["payload"]["text"])
        self.assertIn("credential", report.categories)

    # -- Finding 7: Slack hyphen form + plain session= cookie ------------

    def test_slack_hyphen_form_redacted(self):
        token = "xoxb-1234567890123-abcdefghijklmn-1234567890abcde"
        event = _event(payload={"text": token, "is_complete": True})
        cleaned, report = self.redactor.redact_event(event)
        self.assertNotIn("xoxb-1234567890123", cleaned["payload"]["text"])
        self.assertIn("credential", report.categories)

    def test_slack_underscore_form_redacted(self):
        token = "xoxb_1234567890123-abcdefghijklmn-1234567890abcde"
        event = _event(payload={"text": token, "is_complete": True})
        cleaned, report = self.redactor.redact_event(event)
        self.assertNotIn("xoxb_1234567890123", cleaned["payload"]["text"])

    def test_plain_session_cookie_redacted(self):
        original = "Cookie: session=abc123def456; Path=/"
        event = _event(payload={"text": original, "is_complete": True})
        cleaned, report = self.redactor.redact_event(event)
        self.assertNotIn("abc123def456", cleaned["payload"]["text"])
        self.assertIn("session", report.categories)

    # -- Finding 8: mid-string Authorization incl. Basic -----------------

    def test_mid_string_basic_auth_redacted(self):
        original = 'payload={"headers": {"Authorization": "Basic dXNlcjpwYXNz"}}'
        event = _event(payload={"text": original, "is_complete": True})
        cleaned, report = self.redactor.redact_event(event)
        self.assertNotIn("dXNlcjpwYXNz", cleaned["payload"]["text"])
        self.assertIn("credential", report.categories)


class ReviewFixRound2Tests(unittest.TestCase):
    """Regression tests for Task 3 review round 2 findings (scoped)."""

    def setUp(self):
        self.redactor = Redactor(passthrough=False)

    # -- Round-2 Finding 1 (Medium, load-bearing): multi-token values under
    #    the secret/token/key assignment family must be fully consumed, so
    #    the tail after the first whitespace token never leaks. -----------

    def test_secret_multi_word_full_redaction(self):
        original = "client_secret=correct horse battery staple now; exit"
        event = _event(payload={"text": original, "is_complete": True})
        cleaned, report = self.redactor.redact_event(event)
        self.assertNotIn("horse battery staple", cleaned["payload"]["text"])
        self.assertNotIn("correct horse", cleaned["payload"]["text"])
        self.assertIn("password", report.categories)

    def test_lowercase_secret_multi_word_full_redaction(self):
        original = "secret=super secret key phrase material here"
        event = _event(payload={"text": original, "is_complete": True})
        cleaned, report = self.redactor.redact_event(event)
        self.assertNotIn("super secret", cleaned["payload"]["text"])
        self.assertIn("password", report.categories)

    def test_token_multi_word_full_redaction(self):
        original = "token=I heard the mermaids singing each to each"
        event = _event(payload={"text": original, "is_complete": True})
        cleaned, report = self.redactor.redact_event(event)
        self.assertNotIn("mermaids singing", cleaned["payload"]["text"])
        self.assertIn("credential", report.categories)

    def test_signing_key_multi_word_full_redaction(self):
        original = "signing_key=correct horse battery staple until; rest"
        event = _event(payload={"text": original, "is_complete": True})
        cleaned, report = self.redactor.redact_event(event)
        self.assertNotIn("horse battery staple", cleaned["payload"]["text"])

    def test_access_token_multi_word_full_redaction(self):
        original = "access_token=tanuki perfectly balanced as all things"
        event = _event(payload={"text": original, "is_complete": True})
        cleaned, report = self.redactor.redact_event(event)
        self.assertNotIn("perfectly balanced", cleaned["payload"]["text"])
        self.assertIn("credential", report.categories)

    def test_api_key_multi_word_full_redaction(self):
        original = "api_key=where do they go the gibbons do"
        event = _event(payload={"text": original, "is_complete": True})
        cleaned, report = self.redactor.redact_event(event)
        self.assertNotIn("where do they go", cleaned["payload"]["text"])
        self.assertIn("credential", report.categories)

    def test_refresh_token_multi_word_full_redaction(self):
        original = "refresh_token=to end where i begun and south stays south"
        event = _event(payload={"text": original, "is_complete": True})
        cleaned, report = self.redactor.redact_event(event)
        self.assertNotIn("where i begun", cleaned["payload"]["text"])

    def test_auth_token_multi_word_full_redaction(self):
        original = "auth_token=moses descended from his mountain top"
        event = _event(payload={"text": original, "is_complete": True})
        cleaned, report = self.redactor.redact_event(event)
        self.assertNotIn("descended from", cleaned["payload"]["text"])

    def test_multi_token_stops_at_newline(self):
        original = "client_secret=correct horse battery staple\nexit"
        event = _event(payload={"text": original, "is_complete": True})
        cleaned, report = self.redactor.redact_event(event)
        self.assertNotIn("correct horse", cleaned["payload"]["text"])

    def test_multi_token_stops_at_list_bracket(self):
        original = "token=hello world be like, done] by two"
        event = _event(payload={"text": original, "is_complete": True})
        cleaned, report = self.redactor.redact_event(event)
        self.assertNotIn("hello world be like", cleaned["payload"]["text"])

    def test_genuine_single_token_value_still_redacted(self):
        original = "api_key=sk-0123456789abcdef0123456789abcdef"
        event = _event(payload={"text": original, "is_complete": True})
        cleaned, report = self.redactor.redact_event(event)
        self.assertNotIn("sk-0123456789abcdef0123456789abcdef",
                         cleaned["payload"]["text"])
        self.assertIn("credential", report.categories)

    def test_multi_word_env_secret_still_redacted(self):
        original = "AWS_SECRET_ACCESS_KEY=correct horse battery staple now"
        event = _event(payload={"text": original, "is_complete": True})
        cleaned, report = self.redactor.redact_event(event)
        self.assertNotIn("correct horse battery staple",
                         cleaned["payload"]["text"])

    # -- Round 2 Finding 2 (Low): ``_is_long_sk`` dead condition. Short
    #    bodies (dates/ids/counters) stay in text with uncertain=True; the
    #    real ``sk-ant-api03-…`` body is still redacted.

    def test_round2_short_sk_value_not_redacted_certain(self):
        for token in ("sk-1001", "sk-2024-01-01", "sk-podis"):
            event = _event(payload={"text": f"sts {token}: done",
                                    "is_complete": True})
            cleaned, report = self.redactor.redact_event(event)
            self.assertIn(token, cleaned["payload"]["text"],
                          f"{token} must not be replaced")
            self.assertEqual(report.replaced, 0,
                             f"{token} must not inflate replaced")
            self.assertTrue(report.uncertain)

    def test_round2_genuine_ant_api_sk_token_still_redacted(self):
        token = "sk-ant-api03-0123456789ABCDefghij0123456789ABCDefgh"
        event = _event(payload={"text": token, "is_complete": True})
        cleaned, report = self.redactor.redact_event(event)
        self.assertNotIn("sk-ant-api03", cleaned["payload"]["text"])
        self.assertIn("credential", report.categories)

    def test_round2_report_never_contains_original_text(self):
        original = "client_secret=correct horse battery staple"
        event = _event(payload={"text": original, "is_complete": True})
        _, report = self.redactor.redact_event(event)
        self.assertNotIn("correct", str(report))


class PassthroughDefaultTests(unittest.TestCase):
    """Live-path default: Redactor() skips payload-value substitution."""

    def test_default_passthrough_keeps_secret_shaped_text(self):
        redactor = Redactor()
        original = "Use sk-0123456789abcdef0123456789abcdef to call the API."
        event = _event(payload={"text": original, "is_complete": True})
        clean, report = redactor.redact_event(event)
        self.assertTrue(redactor.passthrough)
        self.assertEqual(clean["payload"]["text"], original)
        self.assertEqual(report.replaced, 0)
        self.assertEqual(report.categories, ())
        self.assertFalse(report.uncertain)
        self.assertEqual(report.details, ())
        meta = clean.get("redaction") or {}
        self.assertEqual(meta.get("state"), "none")
        self.assertEqual(meta.get("replaced"), 0)


if __name__ == "__main__":
    unittest.main()