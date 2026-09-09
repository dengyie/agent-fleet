"""Deterministic event redaction for the agent-fleet supervision surface.

Redaction runs at Agent capture time, before Hub durable ingest and before
any Operator response.  This module is intentionally dependency-light (no
Flask, no cryptography, no network, no filesystem) so both the ``tools/``
side and the Hub can import the same pure rules.

Contract
--------

- ``Redactor.redact_event(event) -> (dict, RedactionReport)`` transforms an
  event's ``payload`` (walking nested dicts/lists) and returns the sanitized
  event plus a bounded report.  The original event must have been validated
  against :mod:`session_schema` already; this module never rejects an
  envelope, it only sanitizes payload values.
- Replacement is deterministic: same input always produces the same output
  and the same report.  Anything iterable that could affect output order is
  sorted (categories/dedup).
- The original text NEVER appears in the ``RedactionReport``: the report only
  carries ``replaced`` count, deduplicated sorted ``categories``, an
  ``uncertain`` flag, and ``details`` tuples of ``(category, replacement)``
  where ``replacement`` never contains the original secret or path.
- High-confidence rules fire first (private key blocks, database URLs, env
  secret assignments, session/cookie ids, bearer tokens, known token sigils).
  A generic absolute path is replaced but flagged ``uncertain``; a sensitive
  location (``.ssh``, ``id_rsa``, ``.key``, ``credentials``, ``.netrc`` ...)
  is certain.
- A whole value under a demonstrably sensitive key (``password=``,
  ``token=``, ``api_key``) is replaced regardless of its format, and the
  replacement is flagged ``uncertain`` when the format cannot be proven
  sensitive.  The same rule applies when that value is itself a container:
  a dict/list under a sensitive key is replaced wholesale (never walked).

Bounds and fail-closed behavior
-------------------------------

- Every string is capped at ``MAX_REDACT_STRING``; nested containers at
  ``MAX_REDACT_DEPTH``.  A value that would overflow either bound is replaced
  by a bounded ``[REDACTED:unclassified]`` placeholder and flagged
  ``uncertain`` -- it is never silently truncated in a way that could let a
  secret survive at the boundary.
- The running replacement counter is capped at ``MAX_REDACT_COUNT`` and the
  report detail list at ``MAX_DETAILS``, so one oversized input can never
  inflate ``replaced`` without bound.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

MAX_REDACT_STRING = 1 << 20        # 1 MiB per-string cap before processing
MAX_REDACT_DEPTH = 64              # nested container depth cap
MAX_REDACT_COUNT = 4096            # cap for the running replacement counter
MAX_DETAILS = 64
MAX_RULES = 64

_PLACEHOLDER_BASE = "[REDACTED:"

_SENSITIVE_PARTS = (
    ".ssh", "id_rsa", "id_ed25519", "_rsa", "_ed25519", ".key",
    "private_key", "credentials", "credential", "cred",
    ".netrc", "auth0", "secret", "token", "password", "passwd",
    ".aws/credentials", "_token", "_apikey", "jenkins",
)

_PASSWORD_KEYS = frozenset({
    "password", "passwd", "pwd", "secret", "client_secret",
    "secret_key", "private_key", "privatekey",
})

# Keys whose whole value must never survive, even when the value is a
# container (dict/list) or carries no recognisable sigil.  A value under one
# of these is replaced and marked ``uncertain=True``, never leaked.
_SENSITIVE_VALUE_KEYS = frozenset({
    "password", "passwd", "pwd", "api_key", "apikey", "api-key",
    "access_token", "accesstoken", "auth_token", "authtoken",
    "refresh_token", "refreshtoken", "client_secret", "clientsecret",
    "secret", "secret_key", "secretkey", "private_key", "privatekey",
    "token", "authorization", "auth", "x-api-key", "x-auth-token",
    "proxy-authorization", "signing_key", "signingkey", "credential",
    "credentials", "session_id", "connect.sid", "sns_credential",
    "k8s_token", "bearer", "aws_secret_access_key",
    "aws_access_key_id",
})


@dataclass(frozen=True)
class RedactionReport:
    """Bounded redaction metadata.

    ``details`` contains only ``(category, replacement)`` pairs where
    ``replacement`` is a fixed placeholder - never the original text.
    """

    replaced: int
    categories: tuple[str, ...]
    uncertain: bool
    details: tuple[tuple[str, str], ...] = ()


class RedactionContext:
    """Working counters for one :meth:`Redactor.redact_event` walk."""

    def __init__(self) -> None:
        self.count = 0
        self.categories: set[str] = set()
        self.uncertain = False
        self.details: list[tuple[str, str]] = []

    def add(self, category: str, replacement: str, *, uncertain: bool) -> None:
        if self.count < MAX_REDACT_COUNT:
            self.count += 1
        self.categories.add(category)
        if uncertain:
            self.uncertain = True
        if len(self.details) < MAX_DETAILS:
            self.details.append((category, replacement))

    def flag_uncertain(self) -> None:
        self.uncertain = True


# ---------------------------------------------------------------------------
# Matching primitives (scanned in a fixed order, first match wins per value)
# ---------------------------------------------------------------------------

_PEM_BLOCK_RE = re.compile(
    r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----.*?"
    r"-----END (?:[A-Z0-9 ]+ )?PRIVATE KEY-----",
    re.DOTALL | re.IGNORECASE,
)

# DB URL scheme://userinfo@... : the userinfo (credentials) spans until the
# first ``@`` that precedes a host.  The ``@`` is consumed by the match so the
# reconstruction ``scheme://[REDACTED:credential]@`` is faithful and one-shot
# (running it twice must not re-fire or double the separator).
_DB_URL_RE = re.compile(
    r"\b(?P<scheme>postgres|postgresql|mysql|mariadb|mongodb(?:\+srv)?|"
    r"redis|rediss|amqp|amqps|mssql)://"
    r"(?P<cred>[^@\s/]+?)@",
    re.IGNORECASE,
)

# Shared assignment-value grammar for the whole secret/token/password family:
# the value is consumed up to the terminator (``;``, ``]``, newline, ``}``)
# or end of line, so "client_secret=correct horse battery staple" is fully
# redacted instead of leaking the tail.  Env, generic secret and password
# assignments below all use this one rule.
_ASSIGN_VALUE_RE = r"[^;\]\n\}]+"

# Env-style secret assignments consume a multi-token value (until ``;``, a
# newline, ``}``/``]``, or end of line), so a password like
# "AWS_SECRET_ACCESS_KEY=correct horse battery staple" is fully redacted.
_ENV_ASSIGN_RE = re.compile(
    r"\b(?P<key>AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY|AWS_SESSION_TOKEN|"
    r"AZURE_CLIENT_SECRET|AZURE_CLIENT_KEY|AZURE_STORAGE_KEY|"
    r"GOOGLE_API_KEY|GITHUB_API_KEY|GH_TOKEN|OPENAI_API_KEY|"
    r"ANTHROPIC_API_KEY|BEDROCK_API_KEY|STRIPE_SECRET_KEY|VPN_TOKEN)"
    r"\s*=\s*(?P<val>" + _ASSIGN_VALUE_RE + r")",
    re.IGNORECASE,
)

# Session / cookie ids: plain ``session=...`` plus the conventional names.
_SESSION_COOKIE_RE = re.compile(
    r"\b(?:connect\.sid|PHPSESSID|JSESSIONID|sessionid|session_id|"
    r"session-id|sid|session)\s*=\s*(?P<val>[A-Za-z0-9_.\-]{4,})",
    re.IGNORECASE,
)

# Generic sensitive secret-family assignments (client_secret=, token=,
# api_key=, ...).  Values share the env/password multi-token grammar, so a
# passphrase like "client_secret=correct horse battery staple" is fully
# consumed (never leaks its tail).
_SECRET_ASSIGN_RE = re.compile(
    r"(?:(?P<key>api[_-]?key|token|access[_ -]?token|refresh[_ -]?token|"
    r"auth_token|signing_key|secret|client_secret)\s*=\s*"
    r"(?P<val>" + _ASSIGN_VALUE_RE + r"))",
    re.IGNORECASE,
)

# Password-family assignments consume a multi-token value the same way as env
# and the secret family (one shared assignment-value grammar).
_PASSWORD_ASSIGN_RE = re.compile(
    r"(?:(?P<key>password|passwd|pwd|secret_key|private_key|privatekey)"
    r"\s*=\s*(?P<val>" + _ASSIGN_VALUE_RE + r"))",
    re.IGNORECASE,
)

# ``sk-`` tokens: a short body (dates, pod ids, counters) must not be treated
# as high-confidence.  ``_is_long_sk`` decides; a short match is kept but the
# report turns ``uncertain``.
_SK_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_])sk-(?P<body>[A-Za-z0-9_\-.]*)",
)

_GITHUB_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:ghp_|ghs_|gho_|github_pat_)[A-Za-z0-9]{8,}",
)
_SLACK_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:xox[bapr]|xapp)[_-][A-Za-z0-9_-]{6,}",
)
_AWS_KEY_RE = re.compile(
    r"(?<![A-Za-z0-9_])AKIA[0-9A-Z]{16,}",
)
_JWT_RE = re.compile(
    r"(?<![A-Za-z0-9_])eyJ[A-Za-z0-9_\-]{12,}\.[A-Za-z0-9._\-]{10,}",
)
_BEARER_RE = re.compile(
    r"\bBearer[ \t]+(?P<tok>[A-Za-z0-9._\-+/=]{6,})",
)
_BASIC_AUTH_RE = re.compile(
    r"\bBasic[ \t]+(?P<tok>[A-Za-z0-9._\-+/=]{4,})",
)
_PATH_ABS_RE = re.compile(
    r"(?<![\w/.:\\])(?:"
    r"(?:[A-Za-z]:\\|\/)(?:[A-Za-z0-9._~\-]+)(?:\\|\/)"
    r"(?:[A-Za-z0-9._~\-]+(?:[\\/][A-Za-z0-9._~\s\-]+)*)|"
    r"~/(?:[A-Za-z0-9._~\-]+(?:/[A-Za-z0-9._~\-]+)*))",
)
_WINDOWS_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9.])(?:[A-Za-z]:\\(?:[A-Za-z0-9._\- ]+\\)"
    r"[A-Za-z0-9._\- ]+|[A-Za-z]:\\[A-Za-z0-9._\-]+)",
)
_HEADER_VALUE_RE = re.compile(
    r"(?m)^[ \t]*(?:Authorization|Proxy-Authorization|X-Api-Key|"
    r"X-Auth-Token)[ \t]*:[ \t]*(?P<value>[^,\r\n]+)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def replacement(category: str) -> str:
    """Return the fixed placeholder used for ``category``."""
    return f"{_PLACEHOLDER_BASE}{category}]"


def _is_sensitive_location(text: str) -> bool:
    lowered = text.lower()
    return any(part.lower() in lowered for part in _SENSITIVE_PARTS)


def _is_long_sk(body: str) -> bool:
    """True when an ``sk-`` body looks like a real token, not a date/pid."""
    if not body:
        return False
    alpha = sum(1 for ch in body if ch.isalnum())
    if alpha >= 24:
        return True
    for prefix in ("ant-api", "proj-", "live-", "test-",
                   "secret-", "admin-", "codex-"):
        if body.startswith(prefix):
            # ant-api03-<long> etc must be long enough to be credible.
            return alpha >= 12
    return False


def _certain_token(text: str) -> bool:
    """True when ``text`` carries a high-confidence credential sigil."""
    if not text:
        return False
    if _GITHUB_RE.search(text) or _SLACK_RE.search(text) or \
            _AWS_KEY_RE.search(text) or _JWT_RE.search(text) or \
            _BEARER_RE.search(text):
        return True
    m = _SK_TOKEN_RE.search(text)
    if m:
        return _is_long_sk(m.group("body"))
    return False


# ---------------------------------------------------------------------------
# String redaction
# ---------------------------------------------------------------------------

def _redact_string(text: str, key_lower: str, ctx: RedactionContext,
                   depth: int) -> str:
    """Redact one bounded string value, deterministic order."""
    if depth > MAX_REDACT_DEPTH:
        ctx.add("unclassified", replacement("unclassified"), uncertain=True)
        return replacement("unclassified")

    # Whole-string handling: this value stands alone (a dict leaf).  If it
    # still exceeds the size bound it must be replaced, not truncated.
    if len(text) > MAX_REDACT_STRING:
        ctx.add("unclassified", replacement("unclassified"), uncertain=True)
        return replacement("unclassified")

    # Empty strings cannot match any token/path/assignment pattern — skip
    # the regex chain (structural empty fields are ~36% of payload values
    # on real transcripts; profiling showed the chain dominating runtime).
    if not text:
        return text

    # A whole value under a demonstrably sensitive key is replaced regardless
    # of whether its format matches a sigil - never leaked, marked uncertain.
    if key_lower in _SENSITIVE_VALUE_KEYS:
        category = "password" if key_lower in _PASSWORD_KEYS else "credential"
        ctx.add(category, replacement(category), uncertain=True)
        return replacement(category)

    result = text
    result = _redact_blocks(result, ctx)
    result = _apply_tokens_and_assignments(result, ctx)
    result = _redact_paths(result, ctx)
    return result


def _redact_blocks(text: str, ctx: RedactionContext) -> str:
    """Handle PEM private-key blocks, DB URLs, env and cookie assignments."""
    out = text

    # PEM / base64 private key blocks
    out, n = _PEM_BLOCK_RE.subn(replacement("private_key"), out)
    if n:
        ctx.add("private_key", replacement("private_key"), uncertain=False)

    # DB URL userinfo -> keep scheme + trailing '@', redact the credentials.
    def _db_repl(match: re.Match) -> str:
        scheme = match.group("scheme")
        ctx.add("db_url", replacement("credential"), uncertain=False)
        return f"{scheme}://{replacement('credential')}@"

    out, _ = _DB_URL_RE.subn(_db_repl, out)

    # Env-style secret assignment (value can be multi-token).
    def _env_repl(match: re.Match) -> str:
        key = match.group("key")
        ctx.add("env", replacement("env"), uncertain=False)
        return f"{key}={replacement('env')}"

    out, _ = _ENV_ASSIGN_RE.subn(_env_repl, out)

    # Session / cookie assignments.
    out, n = _SESSION_COOKIE_RE.subn(replacement("session"), out)
    if n:
        ctx.add("session", replacement("session"), uncertain=False)

    return out


def _apply_tokens_and_assignments(text: str, ctx: RedactionContext) -> str:
    """Redact bearer/basic/sigil tokens and generic assignment values."""
    out = text

    # Bearer / Basic tokens (anywhere, including mid-string).
    def _bearer_repl(match: re.Match) -> str:
        tok = match.group("tok")
        ctx.add("credential", replacement("credential"),
                uncertain=not _certain_token(tok))
        return "Bearer " + replacement("credential")

    out, _ = _BEARER_RE.subn(_bearer_repl, out)

    def _basic_repl(match: re.Match) -> str:
        ctx.add("credential", replacement("credential"), uncertain=False)
        return "Basic " + replacement("credential")

    out, _ = _BASIC_AUTH_RE.subn(_basic_repl, out)

    # sk- tokens: only redact when the body is long enough to be credible;
    # otherwise keep the text but flag the overall redaction as uncertain.
    def _sk_repl(match: re.Match) -> str:
        body = match.group("body")
        if _is_long_sk(body):
            ctx.add("credential", replacement("credential"),
                    uncertain=False)
            return "sk-" + replacement("credential")
        ctx.flag_uncertain()
        return match.group(0)

    out, _ = _SK_TOKEN_RE.subn(_sk_repl, out)

    # High-confidence sigils (github / slack / aws / jwt).
    def _sigil_repl(match: re.Match) -> str:
        ctx.add("credential", replacement("credential"), uncertain=False)
        return replacement("credential")

    for pattern in (_GITHUB_RE, _SLACK_RE, _AWS_KEY_RE, _JWT_RE):
        out, _ = pattern.subn(_sigil_repl, out)

    # Generic single-token assignments (token=, api_key=...).
    def _assign_repl(match: re.Match) -> str:
        key = match.group("key")
        val = match.group("val")
        cat = "password" if key in _PASSWORD_KEYS else "credential"
        strong = _certain_token(val)
        ctx.add(cat, replacement(cat), uncertain=not strong)
        return f"{key}={replacement(cat)}"

    out, _ = _SECRET_ASSIGN_RE.subn(_assign_repl, out)

    # Password assignments with multi-token values.
    def _pass_repl(match: re.Match) -> str:
        key = match.group("key")
        ctx.add("password", replacement("password"), uncertain=True)
        return f"{key}={replacement('password')}"

    out, _ = _PASSWORD_ASSIGN_RE.subn(_pass_repl, out)

    # Header-style auth lines.
    def _header_repl(match: re.Match) -> str:
        header = match.group(0).split(":", 1)[0]
        ctx.add("credential", replacement("credential"), uncertain=False)
        return f"{header}: {replacement('credential')}"

    out, _ = _HEADER_VALUE_RE.subn(_header_repl, out)

    return out


def _redact_paths(text: str, ctx: RedactionContext) -> str:
    def _path_repl(match: re.Match) -> str:
        subject = match.group(0)
        ctx.add("path", replacement("path"),
                uncertain=not _is_sensitive_location(subject))
        return replacement("path")

    out, _ = _PATH_ABS_RE.subn(_path_repl, text)
    out, _ = _WINDOWS_PATH_RE.subn(_path_repl, out)
    return out


# ---------------------------------------------------------------------------
# Redactor (public entrypoint)
# ---------------------------------------------------------------------------

class Redactor:
    """Deterministic payload-value redactor for session events.

    Route A (2026-09-07): the live ingest/query path defaults to
    ``passthrough=True`` so operator consoles see the agent's own
    user/assistant/tool text.  The replacement engine remains available
    for unit tests and any future opt-in call site (``passthrough=False``).
    Envelope allowlisting and size bounds still come from session_schema;
    this flag only skips *payload-value* substitution.
    """

    def __init__(self, *, passthrough: bool = True) -> None:
        self.passthrough = bool(passthrough)

    def redact_event(
        self, event: Mapping[str, Any]
    ) -> tuple[dict, RedactionReport]:
        """Return ``(sanitized_event, report)``.

        ``sanitized_event`` is a shallow copy of the input.  In passthrough
        mode the payload is left intact and the report is empty
        (``state=none``).  Otherwise payload values are replaced by fixed
        placeholders; the report carries only count / categories /
        uncertainty / replacement tuples (never original text).
        """
        out: dict[str, Any] = dict(event)
        if self.passthrough:
            empty = RedactionReport(
                replaced=0, categories=(), uncertain=False, details=())
            out["redaction"] = self._redaction_metadata(empty)
            return out, empty

        ctx = RedactionContext()
        payload = out.get("payload")

        if isinstance(payload, Mapping):
            out["payload"] = self._redact_value(
                dict(payload), "", ctx, 0)
        elif payload is not None and not isinstance(payload, (str, bytes)):
            out["payload"] = self._redact_value(payload, "", ctx, 0)

        report = RedactionReport(
            replaced=ctx.count,
            categories=tuple(sorted(ctx.categories)),
            uncertain=ctx.uncertain,
            details=tuple(ctx.details),
        )
        out["redaction"] = self._redaction_metadata(report)
        return out, report

    def _redact_value(self, value: Any, key_lower: str,
                      ctx: RedactionContext, depth: int) -> Any:
        if depth > MAX_REDACT_DEPTH:
            # Past the safety bound: replace the whole node (never walk into
            # an unbounded subtree) and flag the redaction as uncertain.
            ctx.add("unclassified", replacement("unclassified"),
                    uncertain=True)
            return replacement("unclassified")

        if isinstance(value, str):
            if len(value) > MAX_REDACT_STRING:
                ctx.add("unclassified", replacement("unclassified"),
                        uncertain=True)
                return replacement("unclassified")
            return _redact_string(value, key_lower, ctx, depth + 1)

        if isinstance(value, Mapping):
            if key_lower in _SENSITIVE_VALUE_KEYS:
                category = ("password" if key_lower in _PASSWORD_KEYS
                            else "credential")
                ctx.add(category, replacement(category), uncertain=True)
                return replacement(category)
            return {
                str(k): self._redact_value(v, str(k).lower(), ctx, depth + 1)
                for k, v in value.items()
            }

        if isinstance(value, list):
            if key_lower in _SENSITIVE_VALUE_KEYS:
                category = ("password" if key_lower in _PASSWORD_KEYS
                            else "credential")
                ctx.add(category, replacement(category), uncertain=True)
                return replacement(category)
            return [
                self._redact_value(item, key_lower, ctx, depth + 1)
                for item in value
            ]

        return value

    @staticmethod
    def _redaction_metadata(report: RedactionReport) -> dict[str, Any]:
        if report.replaced:
            state = "redacted"
        elif report.uncertain:
            state = "partial"
        else:
            state = "none"
        return {
            "state": state,
            "uncertain": bool(report.uncertain),
            "rules": list(report.categories)[:MAX_RULES],
            "replaced": report.replaced,
        }


__all__ = [
    "MAX_REDACT_DEPTH",
    "MAX_REDACT_STRING",
    "RedactionReport",
    "Redactor",
    "replacement",
]