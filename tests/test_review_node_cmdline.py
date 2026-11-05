"""Review fix #10: reported instance cmdlines must not carry secrets.

``claude --settings '{"env":{...TOKEN...}}'`` and ``--api-key=...`` used to be
reported verbatim (truncated to 200 chars) to the hub.
"""
from report_schema import redact_cmdline, sanitize_instances


def _row(cmdline):
    return {"pid": 10, "pgid": 10, "exe_path": "/usr/local/bin/claude",
            "cmdline": cmdline, "agent_family": "claude_code",
            "native_file_path": None, "started_at": "", "attachable": False}


def test_settings_json_with_env_tokens_is_redacted():
    cmd = ('/usr/local/bin/claude --settings {"env":{"ANTHROPIC_AUTH_TOKEN":'
           '"tok-abcdef123456","ANTHROPIC_BASE_URL":"https://relay"}} -p hi')
    [row] = sanitize_instances([_row(cmd)])
    assert "tok-abcdef123456" not in row["cmdline"]
    assert row["cmdline"] == "/usr/local/bin/claude --settings [REDACTED:settings] -p hi"


def test_secret_flags_redacted_but_benign_flags_kept():
    cmd = ("codex --api-key=sk-proj-abcdefghijklmnopqrstuv --token abc123 "
           "--max-tokens 100 --no-auth exec --model gpt --password=pw --key k2")
    out = redact_cmdline(cmd)
    for secret in ("sk-proj-abcdefghijklmnopqrstuv", "abc123", "=pw", "k2"):
        assert secret not in out
    assert "--max-tokens 100" in out and "--no-auth exec" in out and "--model gpt" in out
    assert redact_cmdline(out) == out


def test_env_prefix_secret_redacted():
    out = redact_cmdline("OPENAI_API_KEY=abcdef123456 codex exec")
    assert "abcdef123456" not in out


def test_secret_past_200_chars_is_redacted_before_truncation():
    cmd = "claude " + "--add-dir /x " * 14 + "--api-key=sk-ant-" + "A" * 40
    assert len(cmd) > 200 and cmd.index("sk-ant-") < 200
    [row] = sanitize_instances([_row(cmd)])
    assert "sk-ant-" not in row["cmdline"] and len(row["cmdline"]) <= 200


def test_plain_cmdline_unchanged():
    cmd = "/opt/homebrew/bin/claude --resume abc-123 --model opus"
    [row] = sanitize_instances([_row(cmd)])
    assert row["cmdline"] == cmd
