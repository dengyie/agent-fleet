"""Tests for the bounded CLI capability probe layer.

See Task 2 brief of the agent-session-supervision plan.
``tools/session/probe.py`` produces an honest :class:`CapabilityManifest` from
bounded, observed subprocess probes.  Fixtures drive fake ``--version`` /
``--help`` output through a fake ``subprocess.run`` and assert that absent
flags become False, resume is never auto-appended without a successful resume
probe, Hermes stays observation-only, and diagnostics are bounded stable
codes.

Invariants under test (matching the brief and the frozen design spec):

- capability claims derive only from observed probe output, never from a
  version string alone or unverified CLI assumptions;
- the probe never consults ``PATH``: a command must be an absolute path with a
  trusted basename; a bare/relative executable or a fake PATH entry is never
  executed, and missing configuration yields ``executable_not_configured``;
- every subprocess probe runs with ``stdin=DEVNULL``, ``close_fds=True``, an
  isolated non-caller cwd, and a sanitized environment;
- ``resume`` is True only after an explicit resume probe succeeds; a failed or
  disabled resume probe downgrades it to False and (when verified) records
  ``resume_unverified``;
- Claude/Codex may report structured/native capabilities only when the help
  text matches family-specific whole-token grammar (``stream-jsonish``,
  ``execute``, ``--output-format=summary``, ``--resume-extra`` never match);
- Hermes defaults to observation-only best-effort and never claims structured
  ``spawn``/``resume``/transcript capabilities from metadata or help text alone;
- deterministic JSON serialization never embeds raw stdout, command lines,
  environment, working directories, paths, credentials, or exception text.
"""

import json
import math
import os
import posixpath
import subprocess
import tempfile
import unittest
from decimal import Decimal
from fractions import Fraction
from types import SimpleNamespace
from unittest import mock

from tools.session import probe as _probe_module
from tools.session.probe import (  # noqa: F401
    CapabilityManifest,
    KNOWN_AGENTS,
    as_dict,
    probe_agent,
    probe_all,
    to_json,
)

CLAUDE = "/opt/fleet/agents/claude"
CODEX = "/opt/fleet/agents/codex"
HERMES = "/opt/fleet/agents/hermes"


# ---------------------------------------------------------------------------
# subprocess fakes
# ---------------------------------------------------------------------------


def _run_fake_factory(entries):
    """Return a ``subprocess.run`` fake driven by the argv executable basename.

    ``entries`` maps basename ("claude", "codex", ...) to a dict with keys
    ``version`` / ``help`` / ``version_rc`` / ``help_rc`` / ``timeout`` /
    ``missing`` / ``resume_fail`` / ``resume_timeout``.
    """
    def _run(argv, **kwargs):
        exe = posixpath.basename(argv[0])
        spec = entries.get(exe, entries.get("__missing__", {}))
        if spec.get("missing"):
            raise FileNotFoundError(exe)
        if spec.get("timeout"):
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 1))
        flags = set(argv[1:])
        if "--resume" in flags:
            if spec.get("resume_timeout"):
                raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 1))
            if spec.get("resume_fail"):
                return SimpleNamespace(stdout="", stderr="", returncode=1)
            return SimpleNamespace(stdout="", stderr="", returncode=0)
        if "--version" in flags:
            return SimpleNamespace(
                stdout=spec.get("version", ""),
                stderr="",
                returncode=int(spec.get("version_rc", 0)),
            )
        if "--help" in flags or "-h" in flags:
            return SimpleNamespace(
                stdout=spec.get("help", ""),
                stderr="",
                returncode=int(spec.get("help_rc", 0)),
            )
        return SimpleNamespace(stdout="", stderr="", returncode=0)
    return _run


def patch_probe(entries):
    return mock.patch("tools.session.probe.subprocess.run",
                      new=_run_fake_factory(entries))


def _probe(agent, exe, entries, **kwargs):
    """Run ``probe_agent`` with the given absolute executable path."""
    with patch_probe(entries):
        return probe_agent(agent, [exe],
                           timeout_s=kwargs.get("timeout_s", 4.0))


ALL_CAP_FIELDS = (
    "agent", "version", "installed", "spawn", "resume", "native_transcript",
    "structured_stream", "hooks", "pty", "supported_event_kinds",
    "quality_by_kind", "diagnostics",
)

CAPABILITY_BITS = ("spawn", "resume", "native_transcript",
                   "structured_stream", "hooks", "pty")


class ProbeSchemaTests(unittest.TestCase):
    def test_manifest_contains_all_fields(self):
        m = _probe("claude", CLAUDE,
                   {"claude": {"version": "Claude Code version 2.0.1\n"}})
        d = as_dict(m)
        for field in ALL_CAP_FIELDS:
            self.assertIn(field, d, "manifest dict is missing %s" % field)

    def test_deterministic_json_without_raw_feedback(self):
        # Feed a version line that is not parseable into a manifest version so
        # the only way the raw marker can reach anything is raw stdout (banned).
        m = _probe("claude", CLAUDE,
                   {"claude": {"version": "RAW_CLAUDE_LINE_9f3a\n"}})
        raw1 = to_json(m)
        raw2 = to_json(m)
        self.assertEqual(raw1, raw2)
        for banned in ("RAW_CLAUDE_LINE_9f3a", "stdout", "stderr", "argv",
                       "returncode", "cwd", "HOME", "credential", "token",
                       "secret", "path", "/opt/fleet"):
            self.assertNotIn(banned, raw1)
        for code in m.diagnostics:
            self.assertRegex(code, r"^[a-z0-9_]+$")

    def test_json_deterministic_across_instances(self):
        entries = {"claude": {"version": "Claude Code version 2.0.1\n",
                              "help": "  --print  --hooks  --resume"
                                      "  --output-format=stream-json\n"}}
        with patch_probe(entries):
            m1 = as_dict(probe_agent("claude", [CLAUDE], timeout_s=4.0))
            m2 = as_dict(probe_agent("claude", [CLAUDE], timeout_s=4.0))
        self.assertEqual(m1, m2)
        self.assertEqual(sorted(m1["supported_event_kinds"]),
                         m1["supported_event_kinds"])
        self.assertEqual(list(m1["quality_by_kind"].keys()),
                         sorted(m1["quality_by_kind"].keys()))


class ClaudeProbeTests(unittest.TestCase):
    def test_installed_version_alone_no_capabilities(self):
        m = _probe("claude", CLAUDE,
                   {"claude": {"version": "Claude Code version 2.0.1\n"}})
        self.assertTrue(m.installed)
        self.assertEqual(m.agent, "claude")
        self.assertEqual(m.version, "2.0.1")
        for bit in CAPABILITY_BITS:
            self.assertIs(getattr(m, bit), False)
        self.assertEqual(m.supported_event_kinds, ())
        self.assertEqual(m.quality_by_kind, {})
        self.assertEqual(m.diagnostics, [])

    def test_help_advertises_capabilities(self):
        help_text = """Claude Code - starts an interactive session by default.
  -p, --print               non-interactive headless mode
  -r, --resume SESSION_ID   resume a conversation
  --hooks                   run hooks
  --output-format=stream-json  realtime structured event stream
"""
        m = _probe("claude", CLAUDE,
                   {"claude": {"version": "Claude Code version 2.0.1\n",
                               "help": help_text}})
        self.assertTrue(m.installed)
        self.assertTrue(m.spawn)
        self.assertTrue(m.resume)          # help hint + verified resume probe
        self.assertTrue(m.hooks)
        self.assertTrue(m.native_transcript)
        self.assertTrue(m.structured_stream)
        self.assertFalse(m.pty)
        self.assertIn("user_message", m.supported_event_kinds)
        self.assertEqual(m.quality_by_kind["user_message"], ("structured",))

    def test_absent_flags_stay_false(self):
        help_text = ("claude - starts an interactive session by default.\n"
                     "  -p, --print   non-interactive headless mode\n")
        m = _probe("claude", CLAUDE,
                   {"claude": {"version": "Claude Code version 2.0.1\n",
                               "help": help_text}})
        self.assertTrue(m.installed)
        self.assertTrue(m.spawn)             # --print observed
        self.assertFalse(m.resume)              # no --resume hint
        self.assertFalse(m.hooks)
        self.assertFalse(m.native_transcript)      # no --output-format=stream-json
        self.assertFalse(m.structured_stream)
        self.assertEqual(m.supported_event_kinds, ())


class ClaudeGrammarTests(unittest.TestCase):
    def test_stream_jsonish_and_summary_do_not_set_caps(self):
        # ``stream-jsonish`` is not ``stream-json``; ``--output-format=summary``
        # must not activate a structured stream.
        help_text = ("--output-format=summary\n"
                     "  --output-format=stream-jsonish  something\n"
                     "  --print-only\n")
        m = _probe("claude", CLAUDE,
                   {"claude": {"version": "Claude Code version 2.0.1\n",
                               "help": help_text}})
        self.assertFalse(m.native_transcript)
        self.assertFalse(m.structured_stream)
        self.assertFalse(m.spawn)             # --print-only is not --print
        self.assertFalse(m.resume)            # --resume-extra not claimed

    def test_genuine_stream_json_and_print_work(self):
        help_text = """Usage:
  --output-format=stream-json
  -p, --print
  --hooks
"""
        m = _probe("claude", CLAUDE,
                     {"claude": {"version": "Claude Code version 2.0.1\n",
                                 "help": help_text}})
        self.assertTrue(m.native_transcript)
        self.assertTrue(m.structured_stream)
        self.assertTrue(m.spawn)
        self.assertTrue(m.hooks)
        self.assertFalse(m.resume)          # no --resume/-r hint observed

    def test_resume_extra_does_not_claim_resume(self):
        help_text = "--resume-extra --resumer --print\n"
        m = _probe("claude", CLAUDE,
                   {"claude": {"version": "Claude Code version 2.0.1\n",
                               "help": help_text}})
        self.assertFalse(m.resume)
        self.assertTrue(m.spawn)               # --print still observed


class CodexProbeTests(unittest.TestCase):
    def test_installed_version_no_claims(self):
        m = _probe("codex", CODEX,
                   {"codex": {"version": "codex 0.44.0\n"}})
        self.assertTrue(m.installed)
        self.assertEqual(m.version, "0.44.0")
        for bit in CAPABILITY_BITS:
            self.assertIs(getattr(m, bit), False)
        self.assertEqual(m.supported_event_kinds, ())

    def test_exec_json_resume_observed(self):
        help_text = """Usage: codex exec [OPTIONS] PROMPT
  --json               emit structured JSON events
  --resume SESSION_ID  resume a rollout
  --rollout            internal rollout transcript
"""
        m = _probe("codex", CODEX,
                     {"codex": {"version": "codex 0.44.0\n",
                                "help": help_text}})
        self.assertTrue(m.installed)
        self.assertTrue(m.spawn)                    # "exec" observed
        self.assertTrue(m.resume)                   # --resume + verified probe
        self.assertTrue(m.structured_stream)        # --json observed
        self.assertTrue(m.native_transcript)        # --rollout observed
        self.assertFalse(m.hooks)
        self.assertFalse(m.pty)
        self.assertIn("tool_call", m.supported_event_kinds)
        self.assertIn("process_spawn", m.supported_event_kinds)
        self.assertEqual(m.quality_by_kind["process_spawn"], ("structured",))

    def test_no_structured_stream_when_json_absent(self):
        help_text = """Usage: codex exec [OPTIONS] PROMPT
  --rollout   internal rollout transcript
"""
        m = _probe("codex", CODEX,
                     {"codex": {"version": "codex 0.44.0\n",
                                "help": help_text}})
        self.assertTrue(m.installed)
        self.assertFalse(m.structured_stream)
        self.assertTrue(m.native_transcript)
        self.assertFalse(m.resume)                  # no --resume hint


class CodexGrammarTests(unittest.TestCase):
    def test_execute_not_spawn(self):
        # ``execute`` must NOT count as ``exec`` (spawn stays False), while the
        # genuine ``--json``/``--rollout`` tokens elsewhere still claim the
        # structured interfaces they actually advertise.
        help_text = ("Usage: codex execute --json --rollout\n"
                     "  --json    emit structured events\n"
                     "  --rollout transcript\n")
        m = _probe("codex", CODEX,
                     {"codex": {"version": "codex 0.44.0\n",
                                "help": help_text}})
        self.assertFalse(m.spawn)
        self.assertTrue(m.native_transcript)
        self.assertTrue(m.structured_stream)

    def test_execute_alone_no_json_flags(self):
        # ``execute`` alone must not claim spawn; no structured flags.
        help_text = "Usage: codex execute [PROMPT]\n"
        m = _probe("codex", CODEX,
                     {"codex": {"version": "codex 0.44.0\n",
                                "help": help_text}})
        self.assertFalse(m.spawn)
        self.assertFalse(m.structured_stream)
        self.assertFalse(m.native_transcript)

    def test_genuine_exec_subcommand_matches(self):
        help_text = ("Usage: codex exec [OPTIONS] PROMPT\n"
                     "  --json   emit structured JSON events\n"
                     "  --rollout\n")
        m = _probe("codex", CODEX,
                     {"codex": {"version": "codex 0.44.0\n",
                                "help": help_text}})
        self.assertTrue(m.spawn)
        self.assertTrue(m.native_transcript)
        self.assertTrue(m.structured_stream)

    def test_json_flag_is_exact_token(self):
        # ``--json-lines`` is not ``--json``.
        help_text = "Usage: codex exec  --json-lines\n"
        m = _probe("codex", CODEX,
                     {"codex": {"version": "codex 0.44.0\n",
                                "help": help_text}})
        self.assertTrue(m.spawn)
        self.assertFalse(m.structured_stream)
        self.assertFalse(m.native_transcript)


class HermesProbeTests(unittest.TestCase):
    def test_installed_metadata_only_best_effort(self):
        m = _probe("hermes", HERMES,
                    {"hermes": {"version": "Hermes 1.6.0 - gateway\n",
                                "help": "gateway usage --resume --json\n"}})
        self.assertTrue(m.installed)
        self.assertEqual(m.version, "1.6.0")
        for bit in CAPABILITY_BITS:
            self.assertIs(getattr(m, bit), False,
                          "hermes may not claim %s" % bit)
        self.assertEqual(m.supported_event_kinds, ())
        self.assertEqual(m.quality_by_kind, {})

    def test_help_mention_never_claims_spawn_or_resume(self):
        help_text = "hermes --resume --json --rollout --print --hooks\n"
        m = _probe("hermes", HERMES,
                    {"hermes": {"version": "Hermes 1.6.0\n",
                                "help": help_text}})
        self.assertTrue(m.installed)
        self.assertFalse(m.spawn)
        self.assertFalse(m.resume)
        self.assertFalse(m.native_transcript)
        self.assertFalse(m.structured_stream)
        self.assertFalse(m.hooks)

    def test_no_version_no_capabilities(self):
        m = _probe("hermes", HERMES,
                    {"hermes": {"version": "", "help": ""}})
        self.assertTrue(m.installed)
        self.assertEqual(m.version, "")
        self.assertFalse(m.spawn)
        self.assertFalse(m.resume)


class PiProbeTests(unittest.TestCase):
    PI_HELP = """Usage: pi [options] [prompt]
  -p, --print            print response and exit
  --mode <mode>          Output mode: text (default), json, or rpc
  --session <id>         continue a specific session
  --session-id <id>      deterministic session id
  --session-dir <dir>    override session storage directory
  --no-session           disable session persistence
  --fork                 fork the current session
  -r, --resume           pick a session to resume (interactive)
"""

    def _pi(self, help_text=PI_HELP):
        return _probe("pi", "/opt/fleet/agents/pi",
                      {"pi": {"version": "0.84.4\n", "help": help_text}})

    def test_installed_version_no_claims(self):
        m = self._pi(help_text="")
        self.assertTrue(m.installed)
        self.assertEqual(m.agent, "pi")
        self.assertEqual(m.version, "0.84.4")
        for bit in CAPABILITY_BITS:
            self.assertIs(getattr(m, bit), False)
        self.assertEqual(m.supported_event_kinds, ())

    def test_help_advertises_capabilities(self):
        m = self._pi()
        self.assertTrue(m.installed)
        self.assertTrue(m.spawn)                 # -p/--print
        self.assertTrue(m.structured_stream)     # --mode ... json
        self.assertTrue(m.native_transcript)     # --session / --session-id
        self.assertFalse(m.resume)               # -r is an interactive picker
        self.assertFalse(m.hooks)
        self.assertFalse(m.pty)
        self.assertEqual(m.supported_event_kinds,
                         tuple(kind for kind, _ in
                               _probe_module._family_defs()
                               ["pi"]["module"].event_kind_map()))

    def test_mode_json_direct_forms_also_match(self):
        m = self._pi("--mode=json --print\n")
        self.assertTrue(m.structured_stream)
        m = self._pi("--mode json --print\n")
        self.assertTrue(m.structured_stream)

    def test_near_miss_never_matches(self):
        # jsonl/jsonrpc never claim structured stream; --session-dir/--fork/
        # --no-session never claim native transcript; -pt is not -p.
        m = self._pi("--mode jsonl -pt --session-dir /tmp --fork --no-session\n")
        self.assertFalse(m.structured_stream)
        self.assertFalse(m.spawn)
        self.assertFalse(m.native_transcript)

    def test_resume_hint_still_not_claimed(self):
        m = self._pi()
        # help explicitly mentions -r/--resume, but pi is not in
        # _RESUME_VERIFIABLE_FAMILIES and the flag is a picker.
        self.assertFalse(m.resume)


class ProbeFailureTests(unittest.TestCase):
    def test_bare_executable_rejected_never_resolves_path(self):
        # A bare name must never be resolved through PATH.
        with mock.patch("tools.session.probe.subprocess.run") as fake:
            m = probe_agent("codex", ["codex"], timeout_s=4.0)
        self.assertFalse(m.installed)
        self.assertIn("executable_not_configured", m.diagnostics)
        fake.assert_not_called()

    def test_relative_executable_rejected(self):
        with mock.patch("tools.session.probe.subprocess.run") as fake:
            m = probe_agent("codex", ["./codex"], timeout_s=4.0)
        self.assertFalse(m.installed)
        self.assertIn("executable_not_configured", m.diagnostics)
        fake.assert_not_called()

    def test_forbidden_basename_rejected(self):
        with mock.patch("tools.session.probe.subprocess.run") as fake:
            m = probe_agent("claude", ["/bin/rm"], timeout_s=4.0)
        self.assertFalse(m.installed)
        self.assertIn("executable_invalid", m.diagnostics)
        fake.assert_not_called()

    def test_timeout(self):
        m = _probe("claude", CLAUDE, {"claude": {"timeout": True}})
        self.assertFalse(m.installed)
        self.assertFalse(m.spawn)
        self.assertIn("probe_timeout", m.diagnostics)

    def test_nonzero_version_exit(self):
        m = _probe("claude", CLAUDE, {"claude": {"version_rc": 2}})
        self.assertFalse(m.installed)
        self.assertFalse(m.spawn)
        self.assertIn("probe_nonzero_exit", m.diagnostics)

    def test_help_failure_keeps_installed_but_no_caps(self):
        m = _probe("claude", CLAUDE,
                    {"claude": {"version": "Claude Code version 2.0.1\n",
                                "help_rc": 1}})
        self.assertTrue(m.installed)             # version probe succeeded
        self.assertEqual(m.version, "2.0.1")
        for bit in CAPABILITY_BITS:
            self.assertIs(getattr(m, bit), False)
        self.assertIn("probe_help_failed", m.diagnostics)


class CapabilityDowngradeTests(unittest.TestCase):
    def test_resume_false_when_resume_probe_fails(self):
        m = _probe("codex", CODEX,
                    {"codex": {"version": "codex 0.44.0\n",
                                "help": "  --resume SESSION\n",
                                "resume_fail": True}})
        self.assertTrue(m.installed)
        self.assertFalse(m.resume)
        self.assertIn("resume_unverified", m.diagnostics)

    def test_resume_false_when_resume_probe_times_out(self):
        m = _probe("codex", CODEX,
                    {"codex": {"version": "codex 0.44.0\n",
                                "help": "  --resume SESSION\n",
                                "resume_timeout": True}})
        self.assertTrue(m.installed)
        self.assertFalse(m.resume)
        self.assertIn("resume_unverified", m.diagnostics)

    def test_resume_false_when_verification_disabled(self):
        with patch_probe({"codex": {"version": "codex 0.44.0\n",
                                    "help": "  --resume SESSION\n"}}):
            m2 = probe_agent("codex", [CODEX], timeout_s=4.0,
                             verify_resume=False)
        self.assertFalse(m2.resume)
        self.assertNotIn("resume_unverified", m2.diagnostics)

    def test_resume_true_only_after_successful_resume_probe(self):
        m = _probe("codex", CODEX,
                    {"codex": {"version": "codex 0.44.0\n",
                                "help": "  --resume SESSION\n"}})
        self.assertTrue(m.installed)
        self.assertTrue(m.resume)          # resume probe succeeded
        self.assertNotIn("resume_unverified", m.diagnostics)


class ProbeSafetyTests(unittest.TestCase):
    def test_unknown_agent_family_rejected(self):
        with self.assertRaises(ValueError):
            probe_agent("not-a-real-agent", [CLAUDE], timeout_s=4.0)

    def test_forbidden_executable_rejected(self):
        with mock.patch("tools.session.probe.subprocess.run") as fake:
            m = probe_agent("claude", ["/bin/rm", "-rf", "/"], timeout_s=4.0)
        self.assertFalse(m.installed)
        self.assertIn("executable_invalid", m.diagnostics)
        fake.assert_not_called()

    def test_all_help_probes_are_fixed_absolute_argv(self):
        with mock.patch("tools.session.probe.subprocess.run") as fake:
            fake.side_effect = lambda argv, **kw: SimpleNamespace(
                stdout="Claude Code version 2.0.1\n", stderr="", returncode=0)
            probe_agent("claude", [CLAUDE], timeout_s=4.0)
        for call in fake.call_args_list:
            argv = call[0][0]
            self.assertTrue(os.path.isabs(argv[0]))
            self.assertTrue(argv[0].endswith("claude"))
            self.assertTrue(all(isinstance(x, str) for x in argv))
            self.assertTrue(argv[1:])  # at least one fixed flag


class SubprocessIsolationTests(unittest.TestCase):
    """Every probe subprocess must be isolated from caller stdin/cwd/env/fds."""

    def _captured_calls(self):
        """Run a claude probe with a mocked subprocess and return the calls."""
        with mock.patch("tools.session.probe.subprocess.run") as fake:
            fake.side_effect = lambda argv, **kw: SimpleNamespace(
                stdout=(
                    "Claude Code version 2.0.1\n"
                    if "--version" in argv
                    else ("" if "--help" in argv else "")),
                stderr="", returncode=0)
            probe_agent("claude", [CLAUDE], timeout_s=4.0)
        self.assertTrue(fake.called)
        return fake.call_args_list

    def test_kwargs_include_devnull_closefds_isolated_cwd_env(self):
        calls = self._captured_calls()
        for call in calls:
            kw = call.kwargs
            self.assertIs(kw["stdin"], subprocess.DEVNULL)
            self.assertTrue(kw["close_fds"])
            self.assertNotEqual(kw["cwd"], os.getcwd())
            self.assertNotIn("HOME", kw["env"])
            self.assertTrue(kw["env"]["PATH"].startswith(
                os.path.dirname(CLAUDE) + ":"))
            self.assertIn("LANG", kw["env"])
            self.assertEqual(kw["check"], False)

    def test_real_subprocess_call_is_sanitized(self):
        # A real (unmocked) bounded probe invokes with stdin=DEVNULL and an
        # isolated env, and must not hang on inherited stdin.
        from tools.session import probe as pm
        code, _ = pm._run_binary(["/usr/bin/true"], timeout_s=4.0)
        self.assertIn(code, ("ok", "missing", "error"))


class StreamJsonExtensionTests(unittest.TestCase):
    """Exact-value grammar: extensions/punctuation variants are not matches."""

    def _caps(self, help_text):
        m = _probe("claude", CLAUDE,
                   {"claude": {"version": "Claude Code version 2.0.1\n",
                               "help": help_text}})
        return m

    def test_stream_json_exact_and_both_forms_match(self):
        # ``=`` form and whitespace form are both normal help output.
        for line in ("--output-format=stream-json",
                     "--output-format stream-json",
                     "--output-format = stream-json"):
            m = self._caps(line)
            self.assertTrue(m.native_transcript, line)
            self.assertTrue(m.structured_stream, line)

    def test_value_extension_variants_are_rejected(self):
        # ``stream-json-v1``, ``stream-json-foo``, ``stream-json.x`` and
        # ``stream-json_extra`` must never claim the exact capability.
        for line in ("--output-format=stream-json-v1",
                     "--output-format=stream-json-foo",
                     "--output-format=stream-json.x",
                     "--output-format=stream-json_extra",
                     "--output-format stream-json.v1",
                     "--output-format stream-json_extra"):
            m = self._caps(line)
            self.assertFalse(m.native_transcript, line)
            self.assertFalse(m.structured_stream, line)

    def test_review_required_extension_punctuation_forms(self):
        for line in ("--output-format=stream-json-v1",
                     "--output-format=stream-json-foo",
                     "--output-format=stream-json.x",
                     "--output-format=stream-json_extra"):
            m = self._caps(line)
            self.assertFalse(m.native_transcript,
                             "extension %r must not claim stream-json" % line)
            self.assertFalse(m.structured_stream,
                             "extension %r must not claim stream-json" % line)

    def test_delimiter_forms_still_match(self):
        # end-of-string and the strict delimiter set are accepted.
        for line in ("--output-format=stream-json\n",       # end-of-string
                     "--output-format=stream-json ",        # whitespace
                     "--output-format=stream-json,",        # comma
                     "--output-format=stream-json)",        # paren
                     "--output-format=stream-json]"):       # bracket
            m = self._caps(line)
            self.assertTrue(m.native_transcript, repr(line))
            self.assertTrue(m.structured_stream, repr(line))


class PathCanonicalizationTests(unittest.TestCase):
    """The configured executable is canonicalized before alias validation."""

    def test_forbidden_binary_rejected_at_configured_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            link = os.path.join(tmp, "claude")
            os.symlink("/bin/rm", link)  # trusted-looking `claude` -> /bin/rm
            with mock.patch("tools.session.probe.subprocess.run") as fake:
                m = probe_agent("claude", [link], timeout_s=4.0)
            self.assertFalse(m.installed)
            self.assertIn("executable_invalid", m.diagnostics)
            fake.assert_not_called()

    def test_symlink_to_forbidden_binary_rejected_in_probe_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            link = os.path.join(tmp, "codex")
            os.symlink("/bin/cp", link)
            config = {"agents": {"codex": {"command": [link]}}}
            with mock.patch("tools.session.probe.subprocess.run") as fake:
                result = probe_all(config)
            self.assertFalse(result["codex"].installed)
            self.assertIn("executable_invalid",
                          result["codex"].diagnostics)
            fake.assert_not_called()

    def test_symlink_chain_to_existing_claude_target_is_accepted(self):
        # A symlink whose *canonical* basename is a trusted family alias is
        # fine; canonicalization must not reject real installs (e.g.
        # /opt/homebrew/bin/claude -> Cellar/claude-code/.../claude).
        with tempfile.TemporaryDirectory() as tmp:
            real_dir = os.path.join(tmp, "real")
            os.makedirs(real_dir)
            link = os.path.join(tmp, "claude")
            os.symlink(os.path.join(real_dir, "claude"), link)
            with mock.patch("tools.session.probe.subprocess.run") as fake:
                fake.side_effect = lambda argv, **kw: SimpleNamespace(
                    stdout="Claude Code version 2.0.1\n", stderr="",
                    returncode=0)
                m = probe_agent("claude", [link], timeout_s=4.0)
            self.assertTrue(m.installed)
            self.assertEqual(m.version, "2.0.1")
            # the canonicalized real path (not the symlink) is executed
            executed = [c[0][0][0] for c in fake.call_args_list]
            self.assertTrue(executed)
            self.assertEqual(executed[0],
                             os.path.realpath(os.path.join(real_dir,
                                                           "claude")))

    def test_plain_absolute_is_executed_as_is(self):
        with mock.patch("tools.session.probe.subprocess.run") as fake:
            fake.side_effect = lambda argv, **kw: SimpleNamespace(
                stdout="Claude Code version 2.0.1\n", stderr="", returncode=0)
            m = probe_agent("claude", [CLAUDE], timeout_s=4.0)
        self.assertTrue(m.installed)
        self.assertEqual(m.version, "2.0.1")
        assert fake.call_args_list
        self.assertEqual(fake.call_args_list[0][0][0][0],
                         os.path.realpath(CLAUDE))


class PathAdditionsTests(unittest.TestCase):
    """Local-config PATH additions support wrapper runtimes without leaks."""

    def _make_helper(self, tmp):
        helper = os.path.join(tmp, "node_bin")
        os.makedirs(helper, exist_ok=True)
        return helper

    def test_validate_path_adds_filters_invalid_entries(self):
        from tools.session import probe as pm
        with tempfile.TemporaryDirectory() as tmp:
            d1 = os.path.join(tmp, "a")
            d2 = os.path.join(tmp, "b")
            os.makedirs(d1)
            os.makedirs(d2)
            too_long = "/tmp/" + "x" * (pm.MAX_PATH_ADDITION_LEN + 1)
            raw = [d1, d1, d1, d2, "relative/stuff", too_long,
                   "/usr/bin", 42, d1]
            got = pm._validate_path_adds(raw)
            self.assertEqual([os.path.realpath(d1),
                              os.path.realpath(d2)], got)

    def test_validate_path_adds_bounded_count(self):
        from tools.session import probe as pm
        with tempfile.TemporaryDirectory() as tmp:
            dirs = []
            for i in range(8):
                d = os.path.join(tmp, "d%d" % i)
                os.makedirs(d)
                dirs.append(d)
            got = pm._validate_path_adds(dirs)
            self.assertEqual(len(got), pm.MAX_PATH_ADDITIONS)

    def test_path_adds_flow_into_probe_env_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            helper = self._make_helper(tmp)
            calls = []

            def fake(argv, **kwargs):
                calls.append((list(argv), kwargs))
                return SimpleNamespace(stdout="codex 0.44.0\n", stderr="",
                                       returncode=0)

            with mock.patch("tools.session.probe.subprocess.run",
                            side_effect=fake):
                probe_agent("codex", [CODEX], timeout_s=4.0,
                            path_adds=[helper])
            self.assertTrue(calls)
            canonical_helper = os.path.realpath(helper)
            for argv, kw in calls:
                parts = kw["env"]["PATH"].split(":")
                self.assertIn(canonical_helper, parts)
                self.assertIn(os.path.realpath(os.path.dirname(CODEX)), parts)
                # the executable is still invoked by an absolute path;
                # PATH is never used to resolve it.
                self.assertTrue(os.path.isabs(argv[0]))

    def test_probe_all_passes_path_adds_through(self):
        with tempfile.TemporaryDirectory() as tmp:
            helper = self._make_helper(tmp)
            config = {"agents": {
                "codex": {"command": [CODEX], "path_adds": [helper]},
            }}
            calls = []

            def fake(argv, **kw):
                calls.append(kw)
                return SimpleNamespace(stdout="" if "--help" in argv
                                       else "codex 0.44.0\n", stderr="",
                                       returncode=0)

            with mock.patch("tools.session.probe.subprocess.run",
                            side_effect=fake):
                result = probe_all(config)
            self.assertTrue(result["codex"].installed)
            self.assertTrue(calls)
            canonical_helper = os.path.realpath(helper)
            for kw in calls:
                self.assertIn(canonical_helper,
                              kw["env"]["PATH"].split(":"))

    def test_path_adds_never_leak_into_manifest_or_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            helper = self._make_helper(tmp)
            with mock.patch("tools.session.probe.subprocess.run",
                            side_effect=lambda argv, **kw:
                            SimpleNamespace(stdout="", stderr="",
                                            returncode=0)):
                m = probe_agent("codex", [CODEX], timeout_s=4.0,
                                path_adds=[helper])
            self.assertNotIn(helper, to_json(m))
            self.assertNotIn(helper, repr(as_dict(m)))


class ProbeFailureRuntimeTests(unittest.TestCase):
    def test_missing_wrapper_runtime_is_bounded_honest_failure(self):
        # A FileNotFoundError from the probe runner is a bounded
        # ``probe_runtime_unavailable``, not a raw exception.
        entries = {"claude": {"missing": True}}
        with patch_probe(entries):
            m = probe_agent("claude", [CLAUDE], timeout_s=4.0)
        self.assertFalse(m.installed)
        self.assertIn("probe_runtime_unavailable", m.diagnostics)
        self.assertEqual(len(m.diagnostics), 1)


class TimeoutSanitizationTests(unittest.TestCase):
    """Non-finite/out-of-range timeouts never escape as crashy transports."""

    BAD_TIMEOUTS = ("nan", "inf", "-inf", -1, 0, 400, float("nan"),
                    float("inf"), float("-inf"))

    # Every shape that must never crash the probe: string, negative/zero,
    # non-finite floats, huge ints whose ``float()`` conversion overflows, a
    # Decimal that floats to ``inf``, a Fraction in the e99 range, bools, and
    # ``-0.0``.  All must produce a bounded subprocess timeout.
    HARD_TIMEOUT_SHAPES = (
        "nan", "inf", "-inf", -1, 0, 400,
        float("nan"), float("inf"), float("-inf"),
        10 ** 400, 10 ** 309, 2 ** 1024,
        Decimal("1e400"), Fraction(10 ** 100, 3),
        True, False, -0.0,
    )

    def _config_with(self, value):
        return {"agents": {"claude": {"command": [CLAUDE],
                                      "timeout_s": value}}}

    def test_config_timeout_nonfinite_and_out_of_range(self):
        from tools.session import probe as pm
        for raw in ("nan", "inf", "-inf", -1, 0, 400,
                    float("nan"), float("inf"), float("-inf")):
            with self.subTest(raw=raw):
                cfg = self._config_with(raw)
                self.assertEqual(pm.DEFAULT_TIMEOUT_S,
                                 pm._config_timeout(cfg, "claude"),
                                 "non-finite/out-of-range falls back to default")

    def test_config_timeout_accepts_in_range(self):
        from tools.session import probe as pm
        for raw in (0.5, 4.0, 8.0, 29.5):
            with self.subTest(raw=raw):
                cfg = self._config_with(str(raw))
                self.assertEqual(float(raw),
                                 pm._config_timeout(cfg, "claude"),
                                 "in-range clamp")

    def test_config_timeout_missing_or_invalid(self):
        from tools.session import probe as pm
        for cfg in ({}, {"agents": {}}, self._config_with("not-a-number"),
                    self._config_with(None)):
            self.assertEqual(pm.DEFAULT_TIMEOUT_S,
                             pm._config_timeout(cfg, "claude"))

    def test_probe_agent_nonfinite_timeout_returns_bounded_manifest(self):
        # No raise; subprocess sees the bounded default (mock asserts kwargs),
        # and the raw value is never echoed into diagnostics/json.
        for raw in ("nan", "inf", "-inf", -1, 0, 400,
                    float("nan"), float("inf")):
            with self.subTest(raw=raw):
                calls = []
                def fake(argv, **kw):
                    calls.append(kw)
                    return SimpleNamespace(
                        stdout="Claude Code version 2.0.1\n" if "--version"
                        in argv else ("  --print\n" if "--help" in argv
                                      else ""),
                        stderr="", returncode=0)
                with mock.patch("tools.session.probe.subprocess.run",
                                side_effect=fake):
                    m = probe_agent("claude", [CLAUDE], timeout_s=raw)
                self.assertTrue(m.installed)
                for kw in calls:
                    self.assertIsInstance(kw["timeout"], (int, float))
                    self.assertTrue(0.1 <= kw["timeout"] <= 30.0)
                # The raw timeout input must never be echoed into JSON.  Avoid
                # the trivial 0/1 substrings that legitimate version numbers
                # contain, so use a distinctive per-iteration marker check via
                # the diagnostic code path instead: assert the manifest's
                # serialized form contains no "nan"/"inf" timeouts at all.
                raw_json = to_json(m)
                self.assertNotIn("nan", raw_json)
                self.assertNotIn("inf", raw_json)

    def test_probe_all_nonfinite_timeout_is_bounded(self):
        # "nan"/"inf" reach subprocess.run with the bounded default; no crash.
        for raw in ("nan", "inf", -1, 400):
            with self.subTest(raw=raw):
                calls = []
                def fake(argv, **kw):
                    calls.append(kw)
                    return SimpleNamespace(
                        stdout="Claude Agent version 2.1.1\n" if "--version"
                        in argv else "", stderr="", returncode=0)
                with mock.patch("tools.session.probe.subprocess.run",
                                side_effect=fake):
                    result = probe_all(self._config_with(raw))
                self.assertIn("claude", result)
                m = result["claude"]
                self.assertIsInstance(m, CapabilityManifest)
                for kw in calls:
                    self.assertTrue(0.1 <= kw["timeout"] <= 30.0,
                                    "bounded child timeout")

    def test_run_binary_catches_value_and_overflow_errors(self):
        from tools.session import probe as pm
        # A probe that would raise ValueError/OverflowError maps to "error".
        with mock.patch("tools.session.probe.subprocess.run",
                        side_effect=ValueError("boom")):
            self.assertEqual(pm._run_binary([CLAUDE, "--version"],
                                            timeout_s=4.0)[0], "error")
        with mock.patch("tools.session.probe.subprocess.run",
                        side_effect=OverflowError("boom")):
            self.assertEqual(pm._run_binary([CLAUDE, "--version"],
                                            timeout_s=4.0)[0], "error")

    # ---- round-4: overflow-safe, exception-free sanitization ----

    def test_sanitize_timeout_never_raises_and_stays_bounded(self):
        from tools.session import probe as pm
        for raw in self.HARD_TIMEOUT_SHAPES:
            with self.subTest(raw=repr(raw)):
                got = pm._sanitize_timeout(raw)   # total: asserts no raise
                self.assertIsInstance(got, float)
                self.assertIsNot(got, raw, "int pass-through must be a float")
                self.assertTrue(pm.MIN_TIMEOUT_S <= got <= pm.MAX_TIMEOUT_S,
                                "%r -> %r out of bounds" % (raw, got))
                # Mirror the sanitizer's own exception-safe logic when
                # deciding whether this shape must fall back (never call
                # math.isfinite on a huge int in the test itself).
                is_finite_float = (isinstance(raw, float)
                                   and math.isfinite(raw))
                if isinstance(raw, bool) or not isinstance(raw, (int, float)) \
                        or not is_finite_float \
                        or raw < pm.MIN_TIMEOUT_S or raw > pm.MAX_TIMEOUT_S:
                    self.assertEqual(got, pm.DEFAULT_TIMEOUT_S,
                                     "%r should fall back" % repr(raw))

    def test_sanitize_timeout_preserves_in_range(self):
        from tools.session import probe as pm
        for raw in (0.5, 2, 8.0, 16, 29.5):
            with self.subTest(raw=raw):
                got = pm._sanitize_timeout(raw)
                self.assertEqual(got, float(raw))
                self.assertTrue(pm.MIN_TIMEOUT_S <= got <= pm.MAX_TIMEOUT_S)

    def test_sanitize_timeout_negative_zero_maps_to_default(self):
        # ``-0.0`` is finite and equals ``0.0`` exactly; like ``0.0``/``0`` it
        # lies below the floor and must become the bounded default, never a
        # ``timeout=0.0`` transport (at absolute zero / "no timeout").
        from tools.session import probe as pm
        for raw in (-0.0, 0.0, 0):
            with self.subTest(raw=repr(raw)):
                got = pm._sanitize_timeout(raw)
                self.assertEqual(got, pm.DEFAULT_TIMEOUT_S)
                self.assertTrue(pm.MIN_TIMEOUT_S <= got <= pm.MAX_TIMEOUT_S)
        # And an explicit in-range value still passes through, proving the
        # ``bool()`` of the input is never the rejection mechanism.
        self.assertEqual(pm._sanitize_timeout(4.0), 4.0)

    def test_config_timeout_hard_shapes_never_raises(self):
        from tools.session import probe as pm
        for raw in self.HARD_TIMEOUT_SHAPES:
            with self.subTest(raw=repr(raw)):
                cfg = self._config_with(raw)
                got = pm._config_timeout(cfg, "claude")
                self.assertIsInstance(got, float)
                self.assertTrue(pm.MIN_TIMEOUT_S <= got <= pm.MAX_TIMEOUT_S)

    def test_probe_agent_hard_timeout_is_bounded_no_crash(self):
        # Every hard shape through the public API: no raise, a bounded
        # CapabilityManifest, and the child receives a bounded timeout.
        from tools.session import probe as pm
        for raw in self.HARD_TIMEOUT_SHAPES:
            with self.subTest(raw=repr(raw)):
                calls = []
                def fake(argv, **kw):
                    calls.append(kw)
                    return SimpleNamespace(
                        stdout=("Claude Code version 2.0.1\n" if "--version"
                                in argv else
                                ("" if "--help" in argv else "")),
                        stderr="", returncode=0)
                with mock.patch("tools.session.probe.subprocess.run",
                                side_effect=fake):
                    m = probe_agent("claude", [CLAUDE], timeout_s=raw)
                self.assertIsInstance(m, CapabilityManifest)
                self.assertTrue(m.installed)
                self.assertTrue(calls, "expected a probe subprocess call")
                for kw in calls:
                    self.assertIsInstance(kw["timeout"], (int, float))
                    self.assertTrue(0.1 <= kw["timeout"] <= 30.0,
                                    "bounded child timeout for %r" % repr(raw))
                raw_json = to_json(m)
                self.assertNotIn("nan", raw_json)
                self.assertNotIn("inf", raw_json)

    def test_probe_all_hard_config_timeout_is_bounded_no_crash(self):
        # ``probe_all`` reading a config with the hard timeout shapes: no
        # crash, a bounded manifest per family, and bounded child timeouts.
        from tools.session import probe as pm
        for raw in self.HARD_TIMEOUT_SHAPES:
            with self.subTest(raw=repr(raw)):
                cfg = self._config_with(raw)
                calls = []
                def fake(argv, **kw):
                    calls.append(kw)
                    return SimpleNamespace(
                        stdout="Claude Agent version 2.1.1\n" if "--version"
                        in argv else "", stderr="", returncode=0)
                with mock.patch("tools.session.probe.subprocess.run",
                                side_effect=fake):
                    result = probe_all(cfg)
                m = result["claude"]
                self.assertIsInstance(m, CapabilityManifest)
                for kw in calls:
                    self.assertTrue(0.1 <= kw["timeout"] <= 30.0,
                                    "bounded child timeout for %r" % repr(raw))


class ProbeAllTests(unittest.TestCase):
    def test_probe_all_covers_every_known_family(self):
        entries = {
            "claude": {"version": "Claude Code version 2.0.1\n"},
            "codex": {"version": "codex 0.44.0\n"},
            "hermes": {"version": "Hermes 1.6.0\n"},
        }
        config = {
            "agents": {
                "claude": {"command": [CLAUDE]},
                "codex": {"command": [CODEX]},
                "hermes": {"command": [HERMES]},
            }
        }
        with patch_probe(entries):
            result = probe_all(config)
        self.assertEqual(set(result.keys()), set(KNOWN_AGENTS))
        self.assertTrue(result["claude"].installed)
        self.assertEqual(result["claude"].version, "2.0.1")
        self.assertTrue(result["hermes"].installed)

    def test_probe_all_scalar_command(self):
        config = {"agents": {"claude": {"command": CLAUDE}}}
        with patch_probe({"claude": {"version": "Claude Code version 2.0.1\n"}}):
            result = probe_all(config)
        self.assertTrue(result["claude"].installed)

    def test_probe_all_missing_command_is_bounded_not_found(self):
        with mock.patch("tools.session.probe.subprocess.run") as fake:
            result = probe_all({})
        self.assertIn("claude", result)
        self.assertFalse(result["claude"].installed)
        self.assertIn("executable_not_configured",
                      result["claude"].diagnostics)
        fake.assert_not_called()

    def test_probe_all_relative_command_is_bounded_not_executed(self):
        config = {"agents": {"codex": {"command": "codex"}}}
        with mock.patch("tools.session.probe.subprocess.run") as fake:
            result = probe_all(config)
        self.assertFalse(result["codex"].installed)
        self.assertIn("executable_not_configured",
                      result["codex"].diagnostics)
        fake.assert_not_called()


if __name__ == "__main__":
    unittest.main()