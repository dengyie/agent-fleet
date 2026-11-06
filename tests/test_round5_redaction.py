"""Round-5 N2: secret-only redaction covers vendor sigils, keyed values,
CLI flags, URL credentials and mid-line headers without leaking."""

import hashlib
import json

import pytest

from fleet_core.session.redact import Redactor, redact_secrets_text

# Fixtures are assembled at runtime so secret scanners never see a token-shaped
# literal in source; every value here is fake.
SECRET = hashlib.sha256(b"round5-redaction-fixture").hexdigest()[:32]
GOOGLE_KEY = "AI" + "za" + "SyD-9tSrke72PouQMnMX-a7eZSW0jkFMBWY"

LEAK_CASES = {
    "github_pat": (
        "git push https://github_pat_11ABCDEFG0123456789abcd_QWERTYUIOPasdfghjklzx"
        "cvbnm0123456789QWERTYUIOPasdfghjklzx@github.com/o/r",
        ["github_pat_", "QWERTYUIOP"]),
    "github_pat_bare": (
        "GH=github_pat_11ABCDEFG0123456789abcd_QWERTYUIOPasdfghjklzx",
        ["QWERTYUIOP", "11ABCDEFG"]),
    "ghu": ("token ghu_16C7e42F292c6912E7710c838347Ae178B4a", ["16C7e42"]),
    "gho": ("gho_16C7e42F292c6912E7710c838347Ae178B4a", ["16C7e42"]),
    "ghr": ("refresh ghr_16C7e42F292c6912E7710c838347Ae178B4a", ["16C7e42"]),
    "stripe_live": ("STRIPE=sk_" + "live_51H8xYzAbCdEfGhIjKlMnOpQrStUv", ["51H8xYz"]),
    "stripe_restricted": ("use rk_" + "test_51H8xYzAbCdEfGhIjKl now", ["51H8xYz"]),
    "google": ("key " + GOOGLE_KEY,
               ["AIza", "9tSrke72"]),
    "aws_akia": ("AKIAIOSFODNN7EXAMPLE", ["IOSFODNN7"]),
    "aws_asia_pair": (
        "ASIAIOSFODNN7EXAMPLE secret wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        ["IOSFODNN7", "wJalrXUtn"]),
    "glpat_url": ("https://oauth2:" + "gl" + "pat-" + "x" * 20 + "@gitlab.com/x.git",
                  ["glpat", "xxxxxxxxxxxx"]),
    "glpat_bare": ("GITLAB " + "gl" + "pat-" + "AbCdEfGhIjKlMnOpQrSt", ["AbCdEfGh"]),
    "hf": ("hf_AbCdEfGhIjKlMnOpQrStUvWxYz012345", ["AbCdEfGh"]),
    "npm": ("//registry.npmjs.org/:_authToken=npm_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
            ["AbCdEfGh"]),
    "npm_bare": ("NPM npm_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789", ["AbCdEfGh"]),
    "yaml_api_key": ("api_key: " + SECRET, [SECRET]),
    "yaml_suffix_token": ("  github_token: " + SECRET, [SECRET]),
    "yaml_quoted": ('client-secret: "correct horse battery"', ["horse"]),
    "yaml_password_multi": ("db_password: correct horse battery", ["horse", "correct"]),
    "cli_token": ("npx vercel --token " + SECRET, [SECRET]),
    "cli_api_key_eq": ("tool --api-key=" + SECRET + " run", [SECRET]),
    "cli_password_quoted": ('mysql --password "' + "s3cr3t" + ' pass" -h db', ["s3cr3t"]),
    "pg_slash": ("postgres://admin:" + "Ab/cd+ef9==" + "@db.example:5432/app", ["Ab/cd"]),
    "pg_at": ("postgres://admin:" + "p@ssw0rd" + "@db.example/app", ["ssw0rd", "p@ss"]),
    "generic_scheme": ("https://deploy:" + "Zx9/q@w8" + "@registry.example.com/v2/",
                       ["Zx9", "q@w8"]),
    "header_midline": (
        'curl -H "Authorization: token ghp_x" -H "X-Api-Key: ' + SECRET + '" url',
        [SECRET, "ghp_x"]),
    "export_quoted": ('export ANTHROPIC_AUTH_TOKEN="' + SECRET + '"', [SECRET]),
    "env_suffix": ("MY_SERVICE_ACCESS_TOKEN=" + SECRET + " ./run", [SECRET]),
    "json_env": (
        '{"env":{"ANTHROPIC_AUTH_TOKEN":"' + SECRET + '",'
        '"OPENAI_API_KEY":"' + "abc123def456" + "ghi789" + '"}}',
        [SECRET, "abc123def456"]),
    "json_header": (
        '{"headers":{"x-goog-api-key":"' + GOOGLE_KEY + '",'
        '"cookie":"__Secure-next-auth.session-token=abcdef123456"}}',
        ["AIza", "abcdef123456"]),
    "json_embedded_text": (
        'log: {"DB_PASSWORD": "hunter2hunter2", "x": 1} tail',
        ["hunter2"]),
    "python_repr": ("{'stripe_secret': 'whsec_abcdef0123456789'}", ["whsec_"]),
}


@pytest.mark.parametrize("name", sorted(LEAK_CASES))
def test_no_secret_survives(name):
    text, needles = LEAK_CASES[name]
    out, report = redact_secrets_text(text)
    assert report.replaced >= 1, name
    for needle in needles:
        assert needle not in out, (name, out)
    assert "[REDACTED:" in out


def test_json_env_stays_valid_json_and_keeps_structure():
    text = LEAK_CASES["json_env"][0]
    out, _ = redact_secrets_text(text)
    parsed = json.loads(out)
    assert set(parsed["env"]) == {"ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY"}
    assert all(v.startswith("[REDACTED:") for v in parsed["env"].values())


@pytest.mark.parametrize("text", [
    "max_tokens: 100",
    "max_tokens=4096 temperature=0.2",
    "token_count=5",
    '{"usage":{"input_tokens":12,"output_tokens":7,"cache_read_input_tokens":0}}',
    "The token budget was exceeded; retry with fewer tokens.",
    "commit 3f786850e387550fdab836ed7e6dc881de23001b",
    "open http://localhost:5173/@vite/client",
    "see https://example.com/users/@alice for details",
    "npm install @scope/pkg",
    "sort_key: created_at",
    "public_key: ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI",
    "api_key: ${OPENAI_API_KEY}",
    "Error: token expired at 12:00",
    "a::token",
])
def test_benign_text_untouched(text):
    out, report = redact_secrets_text(text)
    assert out == text
    assert report.replaced == 0


def test_secret_redaction_is_idempotent():
    for text, _ in LEAK_CASES.values():
        once, _ = redact_secrets_text(text)
        twice, _ = redact_secrets_text(once)
        assert twice == once


def test_full_redactor_suffix_key_values():
    redactor = Redactor(passthrough=False)
    event = {"payload": {"env": {"ANTHROPIC_AUTH_TOKEN": SECRET,
                                 "MAX_TOKENS": "100"},
                         "usage": {"input_tokens": 3}}}
    clean, report = redactor.redact_event(event)
    assert SECRET not in json.dumps(clean)
    assert clean["payload"]["env"]["MAX_TOKENS"] == "100"
    assert clean["payload"]["usage"] == {"input_tokens": 3}
    assert report.replaced >= 1


def test_hostile_url_like_line_stays_linear():
    import time
    started = time.monotonic()
    redact_secrets_text("/a://b:c" * 20000)
    assert time.monotonic() - started < 3.0
