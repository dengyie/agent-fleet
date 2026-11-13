"""Trusted, manual-only CLI bridge for the disposable Workflow pilot.

No credentials are accepted on argv or emitted to the Hub. Each stage gets a
separate private CLI home and event record. The outer OS launcher confines the
CLI itself as well as its tools; Codex's inner sandbox is therefore disabled.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def write_json(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8") as output:
        json.dump(value, output, ensure_ascii=False, indent=2)
        output.write("\n")
    path.chmod(0o600)


def sandbox_profile(source: Path, scratch: Path, control: Path, config: Path,
                    runtime: Path, codex: Path) -> str:
    """Only this source/control/runtime and public system libraries are readable."""
    roots = {source, scratch, control, config.parent, runtime,
             Path(__file__).resolve().parent, Path(sys.base_prefix).resolve(),
             Path(sys.prefix).resolve(), codex.parent,
             Path("/opt/homebrew"), Path("/usr"), Path("/bin"), Path("/sbin"),
             Path("/System/Library"), Path("/Library/Apple"), Path("/private/etc"),
             Path("/Library/Managed Preferences"), Path("/Library/Preferences"),
             Path("/private/var/db/timezone"), Path("/dev")}
    ancestors = {parent for root in roots for parent in root.parents}
    subpaths = "".join('(subpath ' + json.dumps(str(root)) + ')' for root in sorted(roots))
    literals = "".join('(literal ' + json.dumps(str(root)) + ')' for root in sorted(ancestors))
    writes = "".join('(subpath ' + json.dumps(str(root)) + ')' for root in (scratch, runtime))
    # Developer writes its disposable worktree. Reviewer gets no source writes.
    if os.environ.get("FLEET_PILOT_STAGE") == "develop":
        writes += '(subpath ' + json.dumps(str(source)) + ')'
    return ('(version 1)(deny default)(allow process*)(allow sysctl-read)(allow mach-lookup)(allow user-preference-read)'
            '(allow ipc-posix-shm-read-data (ipc-posix-name "apple.shm.notification_center")'
            '(ipc-posix-name "apple.cfprefs.daemonv1")(ipc-posix-name "apple.cfprefs.' + str(os.getuid()) + 'v1"))'
            '(allow network*)(allow file-read-metadata)(allow file-read-data ' + subpaths + literals + ')'
            '(allow file-write* ' + writes + '(literal "/dev/null")(literal "/dev/tty"))')


def launch(config_path: Path, stage: str, source: Path, control: Path,
           scratch: Path, arguments: list[str]) -> None:
    config = json.loads(config_path.read_bytes())
    runtime = Path(config[stage + "_runtime"]).resolve(strict=True)
    os.environ["FLEET_PILOT_STAGE"] = stage
    profile = sandbox_profile(source.resolve(strict=True), scratch.resolve(strict=True),
                              control.resolve(strict=True), config_path, runtime,
                              Path(config["codex"]).resolve(strict=True))
    profile_path = runtime / "sandbox.sb"
    with profile_path.open("x", encoding="utf-8") as output:
        output.write(profile)
    os.execv("/usr/bin/sandbox-exec", ["/usr/bin/sandbox-exec", "-f", str(profile_path), *arguments])


def review_schema(request: dict) -> dict:
    return {"type": "object", "additionalProperties": False,
            "required": ["schema_version", "source_revision", "subject_digest", "executor", "verdict", "findings"],
            "properties": {
                "schema_version": {"type": "integer", "enum": [1]},
                "source_revision": {"type": "object", "additionalProperties": False,
                    "required": list(request["source_revision"]),
                    "properties": {key: {"type": "string", "enum": [value]}
                                   for key, value in request["source_revision"].items()}},
                "subject_digest": {"type": "string", "enum": [request["subject_digest"]]},
                "executor": {"type": "object", "additionalProperties": False,
                    "required": list(request["executor"]),
                    "properties": {key: {"type": "string", "enum": [value]}
                                   for key, value in request["executor"].items()}},
                "verdict": {"type": "string", "enum": ["passed", "failed", "inconclusive"]},
                "findings": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                    "required": ["code", "severity", "message"], "properties": {
                        "code": {"type": "string"}, "severity": {"type": "string", "enum": ["info", "warning", "error"]},
                        "message": {"type": "string"}}}}}}


def invoke(config_path: Path, stage: str, instruction: str | None = None) -> int:
    config = json.loads(config_path.read_bytes())
    runtime = Path(config[stage + "_runtime"])
    source = Path(os.environ["FLEET_REVIEW_SOURCE"]) if stage == "review" else Path.cwd()
    scratch = Path(os.environ["TMPDIR"]) if stage == "review" else runtime / "scratch"
    # The original adapter deliberately supplies no Hub/Runner credentials.
    environment = {"PATH": "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                   "HOME": str(runtime / "home"), "TMPDIR": str(scratch),
                   "CODEX_HOME": str(runtime / "codex"), "LANG": "en_US.UTF-8"}
    for path, expected in ((Path(config["codex"]), config["cli_sha256"]),
                           (Path(config["native_binary"]), config["native_sha256"]),
                           (runtime / "codex/config.toml", config["runtime_config_sha256"])):
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("pinned_cli_configuration_changed")
    output_path = scratch / "last-message.json"
    argv = [config["codex"], "exec", "--ephemeral", "--ignore-rules", "--json",
            "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox",
            "--color", "never", "-C", str(source), "-o", str(output_path)]
    if stage == "review":
        request = json.loads(Path(os.environ["FLEET_REVIEW_REQUEST"]).read_bytes())
        schema_path = scratch / "review-schema.json"
        write_json(schema_path, review_schema(request))
        argv += ["--output-schema", str(schema_path)]
        prompt = ("Independently review this fixed source against TASK.md and all test_ports.py cases. "
                  "Read ports.py, TASK.md and test_ports.py using read-only tools; do not execute source or change files. "
                  "Check edge cases, validation order and bool/non-string handling. "
                  "Do not delegate or use subagents. Return only the strict JSON review report. "
                  "Use the exact identity and source reference from this trusted request; determine verdict yourself.\n"
                  + json.dumps(request))
        write_json(runtime / "request.json", request)
    else:
        contract = json.loads(instruction or "null")
        if type(contract) is not dict or contract.get("step_key") != "develop":
            raise ValueError("unexpected_developer_contract")
        write_json(runtime / "request.json", contract)
        prompt = ("In this disposable Workflow acceptance repository implement TASK.md by creating only ports.py. "
                  "Read TASK.md and test_ports.py first. Do not change tests/requirements, install dependencies, "
                  "access other repositories, use git, delegate or spawn agents. "
                  "Do not run tests: a separate fixed-test worker does that after independent review. "
                  "Finish with a short explanation. The trusted Workflow identity is:\n" + (instruction or ""))
    argv += [prompt]
    start = time.time()
    write_json(runtime / "invocation.json", {"stage": stage, "started_at": start,
        "source": str(source), "model": config["model"], "provider": config["provider"],
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(), "cli_sha256": config["cli_sha256"]})
    with (runtime / "events.jsonl").open("xb") as stdout, (runtime / "stderr.log").open("xb") as stderr:
        completed = subprocess.run(argv, cwd=source, env=environment, stdin=subprocess.DEVNULL,
                                   stdout=stdout, stderr=stderr, timeout=270)
    write_json(runtime / "completion.json", {"exit_code": completed.returncode,
        "elapsed_seconds": time.time() - start, "final_present": output_path.is_file()})
    if completed.returncode != 0:
        print("real_cli_failed", file=sys.stderr)
        return completed.returncode
    raw = output_path.read_bytes()
    if stage == "review":
        if len(raw) > 65536:
            raise ValueError("oversized_real_review")
        # No verdict rewriting or identity repair: the original adapter validates.
        json.loads(raw)
        (runtime / "report.json").write_bytes(raw)
        sys.stdout.buffer.write(raw)
    else:
        print("real CLI developer completed")
    return 0


def main() -> int:
    mode, config_name, expected_digest, *arguments = sys.argv[1:]
    config_path = Path(config_name).resolve(strict=True)
    if hashlib.sha256(config_path.read_bytes()).hexdigest() != expected_digest:
        raise ValueError("pinned_pilot_configuration_changed")
    config = json.loads(config_path.read_bytes())
    if mode == "develop":
        runtime = Path(config["develop_runtime"])
        launch(config_path, "develop", Path.cwd(), runtime, runtime / "scratch",
               [sys.executable, str(Path(__file__).resolve()), "develop-inner", str(config_path), expected_digest, *arguments])
    elif mode == "launch-review":
        source, control, scratch, *command = arguments
        launch(config_path, "review", Path(source), Path(control), Path(scratch), command)
    elif mode == "review":
        return invoke(config_path, "review")
    elif mode == "develop-inner":
        return invoke(config_path, "develop", arguments[0])
    else:
        raise ValueError("invalid_pilot_mode")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
