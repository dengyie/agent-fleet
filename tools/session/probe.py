"""Bounded runtime capability probes for agent session capture.

This module executes short, memory-safe subprocess probes on the local machine
only.  It:

- never reads a command, argv, environment, working directory, path, or
  credential from the Hub or from untrusted data to build an invocation; every
  invocation is built from an absolute executable path supplied by trusted
  local configuration plus the family's own allowlisted flags (``--version``,
  ``--help``, ``--resume --help``);
- never consults ``PATH``: an executable must be an absolute path (a bare or
  relative name is rejected before any subprocess runs); the path is
  canonicalized with :func:`os.path.realpath` so a trusted-looking symlink to
  another binary is rejected and the family basename must match the configured
  family;
- runs every probe with ``stdin=subprocess.DEVNULL``, ``close_fds=True``, an
  isolated empty cwd, and a sanitized minimal environment so inherited file
  descriptors, the caller's cwd, and environment secrets never reach the
  probed CLI.  A local-config-only list of absolute existing helper directories
  is appended to the sanitized PATH, so Node-wrapper CLIs such as Codex can
  find their runtime without any arbitrary inherited environment;
- never leaks raw stdout/stderr, command lines, environment, working
  directories, paths, credentials, or exception text into the manifest or its
  deterministic JSON; ``diagnostics`` are short stable codes only;
- probes only a fixed allowlist of configured executable families;
- derives every capability claim from observed probe output using
  family-specific full-token/grammar matching (never naive substring tests);
  ``--output-format=summary``, ``stream-jsonish``, ``--output-format=stream-
  json-v1``, ``execute`` and ``--resume-extra`` cannot set any capability,
  while genuine ``--output-format=stream-json``, ``--json``, ``--rollout``,
  ``--resume``, ``--print`` and Codex ``exec`` still can;
- claims ``resume`` only after an explicit resume probe succeeds; a bare
  help-text mention of ``--resume`` is never sufficient;
- keeps Hermes observation-only best-effort: no structured spawn/resume
  capability is ever claimed for Hermes in this phase.

``CapabilityManifest`` is the deterministic, bounded record produced by
``probe_agent`` and ``probe_all``.  It is consumed by the Session Bridge and
Supervisor tasks; its serialized form (``as_dict`` / ``to_json``) is stable
and contains no raw subprocess output.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Any, Mapping, Pattern, Sequence

from tools.session.probes import claude, codex, hermes, pi

DEFAULT_TIMEOUT_S = 8.0
MIN_TIMEOUT_S = 0.1
MAX_TIMEOUT_S = 30.0
MAX_VERSION = 64
MAX_DIAGNOSTICS = 12
# Internal probe read cap.  Parsed help/version text is never stored in the
# manifest; raw "/version"/"/help" output is only used for parsing.
MAX_PROBE_READ = 65536

# Minimal neutral PATH used when the *probed CLI itself* spawns a helper.  The
# probe never resolves the probed executable through PATH.
_PROBE_SYSTEM_PATH = ":".join(("/usr/local/bin", "/usr/bin", "/bin"))

# Bounds for the optional local PATH-additions config.
MAX_PATH_ADDITIONS = 4
MAX_PATH_ADDITION_LEN = 256

# Stable bounded diagnostic codes used by this module.
DIAG_EXECUTABLE_NOT_CONFIGURED = "executable_not_configured"
DIAG_EXECUTABLE_INVALID = "executable_invalid"
DIAG_TIMEOUT = "probe_timeout"
DIAG_NONZERO_EXIT = "probe_nonzero_exit"
DIAG_PROBE_ERROR = "probe_error"
DIAG_HELP_TIMEOUT = "probe_help_timeout"
DIAG_HELP_MISSING = "probe_help_missing"
DIAG_HELP_FAILED = "probe_help_failed"
DIAG_RUNTIME_UNAVAILABLE = "probe_runtime_unavailable"
DIAG_RESUME_UNVERIFIED = "resume_unverified"

_RESUME_VERIFIABLE_FAMILIES = frozenset({"claude", "codex"})


def _family_defs() -> dict[str, dict[str, Any]]:
    """Static family registry: pure module data plus fixed basename allowlists.

    *Aliases* is the set of accepted basenames for a family's executable path;
    anything else is rejected before a subprocess runs.  No alias or argv is
    ever read from a config file or external input.
    """
    return {
        "claude": {
            "module": claude,
            "aliases": frozenset({"claude"}),
        },
        "codex": {
            "module": codex,
            # ``codex.js`` is the canonical basename of the installed Node
            # wrapper script (e.g. /opt/homebrew/bin/codex resolves to a
            # codex.js target); it stays family-specific so a `claude` symlink
            # resolving to another binary is still rejected.
            "aliases": frozenset({"codex", "codex-cli", "codex.js"}),
        },
        "hermes": {
            "module": hermes,
            "aliases": frozenset({"hermes"}),
        },
        "pi": {
            "module": pi,
            "aliases": frozenset({"pi"}),
        },
    }


_FAMILIES: dict[str, dict[str, Any]] = _family_defs()
KNOWN_AGENTS: frozenset[str] = frozenset(_FAMILIES)


@dataclass(frozen=True)
class CapabilityManifest:
    """Bounded, deterministic capability claims for one agent family.

    Capability-truthfulness rules:

    - Every claim is derived from the observed probe output only.  A flag that
      a probe never observed stays ``False`` — even if a version string or help
      text *mentions* a feature name.
    - ``installed`` means the configured executable path resolved and did not
      hard-fail; it does not by itself confer any capture capability.
    - ``spawn``/``resume``/``native_transcript``/``structured_stream``/
      ``hooks``/``pty`` are the capture capabilities.  ``resume`` is never
      auto-appended; it is only True after an explicit resume probe observes
      the interface.
    - ``supported_event_kinds`` / ``quality_by_kind`` exist only when a
      structured interface was observed (``native_transcript`` True or
      ``structured_stream`` True).
    - ``diagnostics`` is a bounded list of short stable codes.
    """

    agent: str
    version: str = ""
    installed: bool = False
    spawn: bool = False
    resume: bool = False
    native_transcript: bool = False
    structured_stream: bool = False
    hooks: bool = False
    pty: bool = False
    supported_event_kinds: tuple[str, ...] = ()
    quality_by_kind: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    diagnostics: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# pure parsing helpers
# ---------------------------------------------------------------------------

def _sanitize_timeout(value: object, /) -> float:
    """Map any timeout input to a safe bounded subprocess timeout.

    Total (never raises) and homomorphic on the public API: a string,
    :class:`bool`, arbitrary object, a non-finite float (``nan``/``inf``), a
    value outside ``[MIN_TIMEOUT_S, MAX_TIMEOUT_S]`` or a huge int all fall
    back to DEFAULT_TIMEOUT_S; a finite ``int``/``float`` inside the range
    returns as its float.  Everything outside the range maps to the default,
    so the result is always bounded in ``[MIN_TIMEOUT_S, MAX_TIMEOUT_S]`` and
    can never reach :func:`subprocess.run` as an invalid/overflowing
    transport.

    The checks are exception-safe *before* any width conversion: the type
    guard rules out strings/``Decimal``/``Fraction``/objects, ``math.isfinite``
    is only ever called on a ``float``, and a huge ``int`` is compared against
    the float bounds directly (Python compares an int to a float exactly
    without converting the int, so ``10**400 > 30.0`` cannot overflow).  The
    original input is never echoed into any diagnostic.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return DEFAULT_TIMEOUT_S
    if isinstance(value, float) and not math.isfinite(value):
        return DEFAULT_TIMEOUT_S
    # ``value`` is a finite int or float from here on.  ``-0.0`` is a finite
    # in-range-adjacent float: like ``0.0`` it lies below the floor and maps
    # to the bounded default, never to a ``timeout=0`` transport.
    if value < MIN_TIMEOUT_S or value > MAX_TIMEOUT_S:
        return DEFAULT_TIMEOUT_S
    return float(value)


def _extract_version(text: str, patterns: Sequence[Pattern[str]]) -> str:
    for pattern in patterns:
        full = pattern.search(text or "")
        if full:
            return (full.group(1) or "").strip()[:MAX_VERSION]
    return ""


def _build_manifest(
    agent: str,
    *,
    installed: bool,
    version: str = "",
    caps: Mapping[str, bool] | None = None,
    kinds: tuple[str, ...] = (),
    qualities: Mapping[str, tuple[str, ...]] | None = None,
    diagnostics: list[str] | None = None,
) -> CapabilityManifest:
    caps = caps or {}
    return CapabilityManifest(
        agent=agent,
        version=version,
        installed=installed,
        spawn=bool(caps.get("spawn", False)),
        resume=bool(caps.get("resume", False)),
        native_transcript=bool(caps.get("native_transcript", False)),
        structured_stream=bool(caps.get("structured_stream", False)),
        hooks=bool(caps.get("hooks", False)),
        pty=bool(caps.get("pty", False)),
        supported_event_kinds=tuple(kinds),
        quality_by_kind=dict(qualities or {}),
        diagnostics=list(diagnostics or [])[:MAX_DIAGNOSTICS],
    )


# ---------------------------------------------------------------------------
# probe subprocess invocation
# ---------------------------------------------------------------------------

_PROBE_ENV = {
    "PATH": _PROBE_SYSTEM_PATH,
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "PYTHONIOENCODING": "utf-8",
}


def _probe_cwd() -> str:
    """Return a bounded, non-caller cwd for probe child processes.

    The caller's cwd must never reach a probed CLI (it may carry project
    secrets).  ``tempfile.gettempdir()`` is a stable sandboxed default.
    """
    return tempfile.gettempdir()


def _env_for(exe_path: str, path_adds: Sequence[str] = ()) -> dict[str, str]:
    """Symmetric sanitized env; the executable's own dir is on PATH for helpers.

    ``path_adds`` is the local-config-only list of validate helper directories
    (see :func:`_validate_path_adds`); they are appended after the executable
    dir so a Node-wrapper CLI can find its runtime without any arbitrary
    inherited environment.  ``PATH`` is always sanitized and rebuilt from
    allowlisted components; it never contains a caller-provided PATH.
    """
    env = dict(_PROBE_ENV)
    parts = []
    exe_dir = os.path.dirname(exe_path)
    if exe_dir:
        parts.append(exe_dir)
    parts.extend(path_adds)
    parts.append(_PROBE_SYSTEM_PATH)
    env["PATH"] = ":".join(parts)
    return env


def _run_binary(
    argv: Sequence[str],
    timeout_s: float,
    path_adds: Sequence[str] = (),
) -> tuple[str, str]:
    """Run one bounded probe; returns ``(code, bounded_stdout)``.

    ``code`` is one of ``"ok"``, ``"missing"``, ``"timeout"``, ``"nonzero"``
    or ``"error"``.  Every call uses an isolated subprocess cwd, a sanitized
    environment, closed inherited descriptors and ``stdin=DEVNULL`` so no
    caller files, env secrets, or inherited stdin reach the probed CLI.  Read
    errors, invalid timeouts and transports are mapped to stable codes, never
    raw exceptions.  The timeout is sanitized here as a second safety net (the
    public entry points already sanitize); ``_sanitize_timeout`` is total, so
    this call can never raise, and the explicit ``except ValueError/
    OverflowError`` below is a transport-level backstop only.
    """
    argv = list(argv)
    if not argv or not isinstance(argv[0], str):
        return "error", ""
    timeout_s = _sanitize_timeout(timeout_s)
    env = _env_for(argv[0], path_adds)
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            close_fds=True,
            cwd=_probe_cwd(),
            env=env,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except FileNotFoundError:
        return "missing", ""
    except subprocess.TimeoutExpired:
        return "timeout", ""
    except (subprocess.SubprocessError, OSError,
            UnicodeDecodeError, ValueError, OverflowError):
        return "error", ""
    if result.returncode != 0:
        return "nonzero", ""
    return "ok", (result.stdout or "")[:MAX_PROBE_READ]


def _resume_probe(
    exe_path: str, timeout_s: float, path_adds: Sequence[str] = ()
) -> bool:
    """Verify a genuine resume path; only a success yields ``True``.

    Any failure (missing, timeout, non-zero exit, transport, decode) resolves
    to False and ``resume`` stays False.  Uses the same isolated invocation.
    """
    code, _stdout = _run_binary(
        [exe_path, "--resume", "--help"], timeout_s, path_adds=path_adds
    )
    return code == "ok"


# ---------------------------------------------------------------------------
# the public probe
# ---------------------------------------------------------------------------

def _validate_command(
    agent: str,
    command: Sequence[str],
) -> tuple[str | None, str | None]:
    """Return ``(exe_path, diagnostic)`` or ``(None, diagnostic)``.

    Requires ``command[0]`` to be a non-empty absolute path whose canonical
    basename (after :func:`os.path.realpath`) is an allowed alias for the
    family; a trusted-looking symlink that resolves to a different binary is
    rejected before any subprocess runs.  Bare/relative names, unusable types
    and forbidden basenames get a bounded diagnostic.
    """
    family = _FAMILIES[agent]
    if not isinstance(command, Sequence) or isinstance(command, (str, bytes)):
        return None, DIAG_EXECUTABLE_INVALID
    command = list(command)
    if not command or not all(isinstance(c, str) and c for c in command):
        return None, DIAG_EXECUTABLE_INVALID
    exe = command[0].strip()
    if not exe or not os.path.isabs(exe):
        return None, DIAG_EXECUTABLE_NOT_CONFIGURED
    try:
        canonical = os.path.realpath(exe)
    except (OSError, ValueError):
        canonical = exe
    if os.path.basename(canonical) not in family["aliases"]:
        return None, DIAG_EXECUTABLE_INVALID
    return canonical, None


def _validate_path_adds(raw: Any) -> list[str]:
    """Validate the local PATH-additions list into a bounded, deduped list.

    Accepts a sequence of absolute, existing directories only; anything else
    is dropped (never an error).  At most ``MAX_PATH_ADDITIONS`` entries of at
    most ``MAX_PATH_ADDITION_LEN`` bytes are kept, duplicates and the default
    system dirs are removed, and entries are canonicalized so the probe env
    stays symmetric with the executable path.  The result is used only to
    build the sanitized child PATH; it is never exposed in the manifest.
    """
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        return []
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        item = item.strip()
        if not item or len(item) > MAX_PATH_ADDITION_LEN:
            continue
        if not os.path.isabs(item):
            continue
        try:
            if not os.path.isdir(item):
                continue
        except OSError:
            continue
        canonical = os.path.realpath(item)
        if canonical in out:
            continue
        if canonical in _PROBE_SYSTEM_PATH.split(":"):
            continue
        out.append(canonical)
        if len(out) >= MAX_PATH_ADDITIONS:
            break
    return out


def probe_agent(
    agent: str,
    command: Sequence[str],
    timeout_s: float = DEFAULT_TIMEOUT_S,
    verify_resume: bool = True,
    path_adds: Sequence[str] = (),
) -> CapabilityManifest:
    """Probe one configured agent family and return a bounded manifest.

    ``command`` must be a non-empty sequence of strings whose first element is
    an absolute executable path whose canonical basename (after
    ``os.path.realpath``) belongs to the family.  ``timeout_s`` may be any
    value (string, bool, nan/inf, negative, ``0``, a huge int, or a
    Decimal/Fraction); it is sanitized by :func:`_sanitize_timeout` to a
    bounded value in ``[MIN_TIMEOUT_S, MAX_TIMEOUT_S]`` before any subprocess
    runs and can never crash the probe.  ``path_adds`` is a validated
    local-config-only list of absolute existing helper directories appended to
    the sanitized probe PATH (e.g. a Node bin dir for a codex wrapper); the
    probe only ever appends the family's fixed readonly flags (``--version``,
    ``--help``, and a ``--resume --help`` verification probe); it never runs a
    command built from external/Hub data and never resolves through PATH.
    """
    if agent not in KNOWN_AGENTS:
        raise ValueError(f"unknown agent family: {agent!r}")
    module = _FAMILIES[agent]["module"]
    path_adds = _validate_path_adds(path_adds)
    # Normalize any public-API timeout input (strings, bools, ``nan``/``inf``,
    # negative, ``0``, huge ints) to a safe bounded value before it can reach
    # subprocess.run.  Total: never raises.
    timeout_s = _sanitize_timeout(timeout_s)

    exe_path, diagnostic = _validate_command(agent, command)
    if exe_path is None:
        return _build_manifest(agent, installed=False, diagnostics=[diagnostic])
    diagnostics: list[str] = []

    # ---- version probe ------------------------------------------------
    v_code, v_text = _run_binary(
        [exe_path, "--version"], timeout_s, path_adds=path_adds
    )
    if v_code != "ok":
        # A FileNotFoundError (``missing``) means the absolute executable or
        # its wrapper runtime could not be launched in the sanitized env; that
        # is a bounded, honest probe failure for the configured agent.
        code = {
            "missing": DIAG_RUNTIME_UNAVAILABLE,
            "timeout": DIAG_TIMEOUT,
            "nonzero": DIAG_NONZERO_EXIT,
            "error": DIAG_PROBE_ERROR,
        }.get(v_code, DIAG_NONZERO_EXIT)
        return _build_manifest(agent, installed=False, diagnostics=[code])
    version = _extract_version(v_text, module.version_patterns())

    # ---- help probe --------------------------------------------------
    h_code, h_text = _run_binary(
        [exe_path, "--help"], timeout_s, path_adds=path_adds
    )
    if h_code == "ok":
        caps = module.match(h_text)
    else:
        caps = {
            "spawn": False, "resume": False, "native_transcript": False,
            "structured_stream": False, "hooks": False, "pty": False,
        }
        if h_code == "timeout":
            diagnostics.append(DIAG_HELP_TIMEOUT)
        elif h_code == "missing":
            diagnostics.append(DIAG_HELP_MISSING)
        else:
            diagnostics.append(DIAG_HELP_FAILED)

    # ---- resume is never auto-appended ------------------------------
    if (agent in _RESUME_VERIFIABLE_FAMILIES and verify_resume
            and caps.get("resume")):
        if _resume_probe(exe_path, timeout_s, path_adds):
            caps["resume"] = True
        else:
            caps["resume"] = False
            diagnostics.append(DIAG_RESUME_UNVERIFIED)
    elif caps.get("resume"):
        # Non-verifiable family or verify_resume disabled: help text alone is
        # never a resume claim.
        caps["resume"] = False

    # ---- structured event grid --------------------------------------
    native = caps.get("native_transcript", False)
    structured = caps.get("structured_stream", False)
    supported: tuple[str, ...] = ()
    qualities: dict[str, tuple[str, ...]] = {}
    if native or structured:
        kinds = module.event_kind_map()
        supported = tuple(kind for kind, *_ in kinds)
        qualities = {kind: tuple(quality for quality in quality_tuple)
                     for kind, quality_tuple in kinds}

    return _build_manifest(
        agent,
        installed=True,
        version=version,
        caps=caps,
        kinds=supported,
        qualities=qualities,
        diagnostics=diagnostics,
    )


# ---------------------------------------------------------------------------
# serialization
# ---------------------------------------------------------------------------

def as_dict(manifest: CapabilityManifest) -> dict[str, Any]:
    """Deterministic, bounded public dict for one manifest.

    Emits only the manifest's allowlisted fields.  Raw output (stdout/stderr,
    argv, env, cwd, paths, credentials) and exception text can never appear;
    ``diagnostics`` carries only short stable codes.  All maps/lists are
    sorted for determinism.
    """
    return {
        "agent": manifest.agent,
        "version": manifest.version,
        "installed": manifest.installed,
        "spawn": manifest.spawn,
        "resume": manifest.resume,
        "native_transcript": manifest.native_transcript,
        "structured_stream": manifest.structured_stream,
        "hooks": manifest.hooks,
        "pty": manifest.pty,
        "supported_event_kinds": sorted(manifest.supported_event_kinds),
        "quality_by_kind": {
            kind: sorted(quals)
            for kind, quals in sorted(manifest.quality_by_kind.items())
        },
        "diagnostics": list(manifest.diagnostics)[:MAX_DIAGNOSTICS],
    }


def to_json(manifest: CapabilityManifest, *, sort_keys: bool = True) -> str:
    """Deterministic JSON (``sort_keys=True`` by default)."""
    return json.dumps(as_dict(manifest), ensure_ascii=True, sort_keys=sort_keys)


# ---------------------------------------------------------------------------
# probe_all
# ---------------------------------------------------------------------------

def _config_timeout(config: Mapping[str, Any], agent: str) -> float:
    """Read ``agents.<agent>.timeout_s`` from a runner-style config if present.

    Only the timeout is read; the config's ``command`` is never trusted by the
    probe runner.  Invalid/absent/non-finite/un-convertible values (including
    a huge ``int`` whose ``float()`` conversion overflows) fall back to
    DEFAULT_TIMEOUT_S and out-of-range values are mapped to the default, so a
    ``"nan"``, ``"inf"`` or ``10**400`` config can never reach
    ``subprocess.run`` as a crashing transport.
    """
    entry = None
    if isinstance(config, Mapping):
        agents = config.get("agents")
        if isinstance(agents, Mapping):
            entry = agents.get(agent)
    if not isinstance(entry, Mapping):
        return DEFAULT_TIMEOUT_S
    # ``float()`` can raise on a huge int (``10**400``), a non-numeric string
    # or an object; all of those route to the bounded default.  The result is
    # then range/non-finite sanitized, so a ``"nan"``/``"inf"``/huge config
    # can never reach ``subprocess.run`` as a crashing transport.
    try:
        value = float(entry.get("timeout_s", DEFAULT_TIMEOUT_S))
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_TIMEOUT_S
    return _sanitize_timeout(value)


def _config_command(
    config: Mapping[str, Any],
    agent: str,
) -> list[str] | None:
    """Read the trusted absolute executable for ``agent`` from local config.

    Accepts either a sequence (``["/abs/path/claude"]``) or a documented
    scalar form (``command: /abs/path/claude``).  Returns ``None`` when
    absent/invalid so the caller can emit ``executable_not_configured``; a
    bare/relative path is left for ``probe_agent`` to reject without any
    PATH resolution.
    """
    if not isinstance(config, Mapping):
        return None
    agents = config.get("agents")
    if not isinstance(agents, Mapping):
        return None
    entry = agents.get(agent)
    if not isinstance(entry, Mapping):
        return None
    raw = entry.get("command")
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = raw.strip()
        return [raw] if raw else None
    if isinstance(raw, (list, tuple)):
        if not raw or not all(isinstance(c, str) for c in raw):
            return None
        return list(raw)
    return None


def _config_path_adds(
    config: Mapping[str, Any],
    agent: str,
) -> list[str]:
    """Read the optional local PATH-additions list for ``agent``.

    Shape: ``agents.<agent>.path_adds = ["/abs/helper_dir", ...]`` — a list of
    absolute, existing directories added to the sanitized probe PATH so
    wrapper CLIs (e.g. a Node-based Codex) can find their runtime.  Entries are
    validated/bounded/deduped by :func:`_validate_path_adds`; invalid entries
    are dropped.  This is purely local config, the list is never serialized
    into a manifest, and it can never look up the configured executable itself
    (the executable path is always canonical and absolute).
    """
    if not isinstance(config, Mapping):
        return []
    agents = config.get("agents")
    if not isinstance(agents, Mapping):
        return []
    entry = agents.get(agent)
    if not isinstance(entry, Mapping):
        return []
    return _validate_path_adds(entry.get("path_adds"))


def probe_all(config: Mapping[str, Any]) -> dict[str, CapabilityManifest]:
    """Probe every known family and return ``agent -> manifest``.

    ``config`` is a runner-style mapping (e.g. ``{"agents": {...}}``).  The
    executable for each family is read from the local ``agents.<agent>``
    ``.command`` as an absolute path (sequence or scalar); the optional
    ``agents.<agent>.path_adds`` list (absolute existing helper directories)
    augments the sanitized probe PATH for wrapper CLIs.  When ``command`` is
    absent/invalid/relative, the family returns ``installed=False`` with
    ``executable_not_configured`` or ``executable_invalid``; PATH is never
    consulted for the executable and a bare name is never executed.  Returns a
    dict keyed by family name.
    """
    out: dict[str, CapabilityManifest] = {}
    for agent in sorted(KNOWN_AGENTS):
        timeout_s = _config_timeout(config, agent)
        command = _config_command(config, agent)
        if command is None:
            out[agent] = _build_manifest(
                agent, installed=False,
                diagnostics=[DIAG_EXECUTABLE_NOT_CONFIGURED])
            continue
        out[agent] = probe_agent(
            agent, command, timeout_s=timeout_s,
            path_adds=_config_path_adds(config, agent))
    return out


__all__ = [
    'KNOWN_AGENTS',
    'CapabilityManifest',
    'as_dict',
    'probe_agent',
    'probe_all',
    'to_json',
]