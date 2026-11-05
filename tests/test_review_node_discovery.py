"""Review fixes #3/#4/#6 for pure-local discovery.

#3 Codex rollout first lines are ~20KB (base_instructions): the resume binding
   must read the whole session_meta line.
#4 macOS Electron/Chromium helpers (``--type=``), unquoted spaced paths and the
   truncated ``comm`` column must not produce false agent instances; argv[1]
   only counts for interpreters.
#6 native transcript lookup uses the known layout and keeps working beyond the
   old 512-file whole-tree cap.
"""
import json
import os

from tools.probe import discovery

UUID = "019a0000-1111-7222-8333-444455556666"


# ---------------------------------------------------------------- #3 ----

def _write_rollout(root, uuid=UUID, meta_id=None):
    day = root / ".codex" / "sessions" / "2026" / "11" / "05"
    day.mkdir(parents=True, exist_ok=True)
    path = day / f"rollout-2026-11-05T10-00-00-{uuid}.jsonl"
    meta = {"type": "session_meta", "payload": {
        "id": meta_id or uuid, "cwd": "/tmp/x",
        "base_instructions": {"text": "x" * 25_000}}}
    line = json.dumps(meta)
    assert len(line) > 25_000
    path.write_text(line + "\n" + json.dumps({"type": "event_msg"}) + "\n",
                    encoding="utf-8")
    return path


def test_codex_resume_binds_with_25kb_session_meta(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    path = _write_rollout(tmp_path)
    assert discovery._codex_native_path(f"codex resume {UUID}") == str(path)
    assert discovery._codex_native_path(f"codex --resume {UUID}") == str(path)


def test_codex_resume_rejects_mismatched_meta_id(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    _write_rollout(tmp_path, meta_id="019a0000-9999-7222-8333-444455556666")
    assert discovery._codex_native_path(f"codex resume {UUID}") is None


# ---------------------------------------------------------------- #6 ----

def test_claude_session_found_among_more_than_512_files(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    projects = tmp_path / ".claude" / "projects"
    for p in range(30):
        proj = projects / f"-Users-me-proj{p}"
        proj.mkdir(parents=True)
        for n in range(20):
            (proj / f"other-{p}-{n}.jsonl").write_text("{}\n")
    target = projects / "-Users-me-proj17" / "abc-session.jsonl"
    target.write_text("{}\n")
    assert discovery._claude_native_path("claude --resume abc-session") == str(target)


def test_codex_rollout_found_among_more_than_512_files(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    for d in range(1, 29):
        day = tmp_path / ".codex" / "sessions" / "2026" / "10" / f"{d:02d}"
        day.mkdir(parents=True)
        for n in range(20):
            (day / f"rollout-2026-10-{d:02d}T00-00-00-other{n}.jsonl").write_text("{}\n")
    path = _write_rollout(tmp_path)
    assert discovery._codex_native_path(f"codex resume {UUID}") == str(path)


def test_duplicate_native_name_is_ambiguous(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    projects = tmp_path / ".claude" / "projects"
    for name in ("a", "b"):
        (projects / name).mkdir(parents=True)
        (projects / name / "dup.jsonl").write_text("{}\n")
    assert discovery._claude_native_path("claude --resume dup") is None


# ---------------------------------------------------------------- #4 ----

ZCODE_RENDERER = (
    "/Applications/ZCode.app/Contents/Frameworks/ZCode Helper (Renderer).app/"
    "Contents/MacOS/ZCode Helper (Renderer) --type=renderer "
    "--user-data-dir=/Users/me/Library/Application Support/ZCode --lang=en-US")
ZCODE_HELPER = (
    "/Applications/ZCode.app/Contents/Frameworks/ZCode Helper.app/Contents/"
    "MacOS/ZCode Helper --type=utility --utility-sub-type=network.mojom.NetworkService")
CODEX_SERVICE = (
    "/Applications/ChatGPT.app/Contents/Frameworks/Codex Framework.framework/"
    "Versions/156.0.8078.79/Helpers/Codex (Service).app/Contents/MacOS/"
    "Codex (Service) --type=utility --utility-sub-type=storage.mojom.StorageService")
CODEX_RENDERER = (
    "/Applications/ChatGPT.app/Contents/Frameworks/Codex Framework.framework/"
    "Versions/156.0.8078.79/Helpers/Codex (Renderer).app/Contents/MacOS/"
    "Codex (Renderer) --type=renderer --user-data-dir=/Users/me/Library/"
    "Application Support/Codex")

HELPER_LINES = (ZCODE_RENDERER, ZCODE_HELPER, CODEX_SERVICE, CODEX_RENDERER)


def _ps_text(lines):
    return "".join(f"me {100 + i} 1 {100 + i} {cmd}\n"
                   for i, cmd in enumerate(lines))


def _rows(monkeypatch, lines):
    monkeypatch.setattr(discovery.os, "name", "posix")
    monkeypatch.setattr(discovery, "_darwin_exe_path", lambda pid: "")

    def fake_ps(argv, timeout=discovery.PS_TIMEOUT_S):
        if argv[1] == discovery._PS_FIELDS:
            return _ps_text(lines)
        return ""

    monkeypatch.setattr(discovery, "_run_ps", fake_ps)
    return discovery.enumerate_process_rows()


def test_macos_electron_helpers_are_not_agents(monkeypatch):
    rows = _rows(monkeypatch, HELPER_LINES)
    assert len(rows) == len(HELPER_LINES)
    # 含空格的 argv 未被拆错列：cmdline 是完整命令
    assert [r.cmdline for r in rows] == list(HELPER_LINES)
    assert [discovery.classify_family(r.exe_path, r.cmdline) for r in rows] == [None] * 4


def test_genuine_cli_processes_still_detected(monkeypatch):
    lines = (
        "node /usr/local/bin/codex exec --json hi",
        "/opt/homebrew/bin/claude --settings {}",
        "claude",
        "python3 /opt/tools/hermes chat",
        "node /opt/cli/zcode.cjs -p hi",
    )
    rows = _rows(monkeypatch, lines)
    got = [discovery.classify_family(r.exe_path, r.cmdline) for r in rows]
    assert got == ["codex", "claude_code", "claude_code", "hermes", "zcode"]


def test_argv1_ignored_for_non_interpreters():
    # 非解释器的 argv[1] 恰好是家族名，不得被识别
    assert discovery.classify_family("/usr/bin/less", "less codex") is None
    assert discovery.classify_family(
        "/bin/zsh", "/bin/zsh /Users/me/bin/claude") is None
    assert discovery.classify_family(
        "/usr/bin/node", "node /usr/local/bin/codex") == "codex"
    assert discovery.classify_family(
        "/usr/bin/python3.12", "python3.12 /opt/x/hermes") == "hermes"


def test_spaced_absolute_argv0_resolves_existing_file(tmp_path):
    app = tmp_path / "My App Helper (Renderer).app" / "Contents" / "MacOS"
    app.mkdir(parents=True)
    exe = app / "My App Helper (Renderer)"
    exe.write_text("")
    args = f"{exe} --type=renderer --flag value"
    assert discovery._spaced_abs_argv0(args) == os.path.realpath(str(exe))
    assert discovery._spaced_abs_argv0("relative path here") == ""


def test_darwin_exe_path_for_self():
    # 发布证据不允许 skip：macOS 走真实 proc_pidpath，其它平台断言安全返回空串。
    path = discovery._darwin_exe_path(os.getpid())
    if discovery.sys.platform.startswith("darwin"):
        assert path and os.path.isabs(path) and os.path.exists(path)
    else:
        assert path == ""
