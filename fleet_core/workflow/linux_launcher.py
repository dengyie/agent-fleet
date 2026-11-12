"""Linux namespace launcher for registered Workflow test and review commands.

Use an absolute script path with isolated Python; no Hub, Node or test imports
are needed. ``--fixed-test`` derives paths from the current runtime/workdir.
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import errno
import os
from pathlib import Path
import shutil
import sys

_BPF_LIMIT = 64 * 1024
_NETWORK_SYSCALLS = (
    "socket", "socketpair", "connect", "bind", "listen", "accept", "accept4",
    "sendto", "sendmsg", "sendmmsg", "recvfrom", "recvmsg", "recvmmsg",
    "shutdown", "getsockname", "getpeername", "setsockopt", "getsockopt",
)


def _fixed_test_arguments(argv: list[str]) -> list[str]:
    parser = argparse.ArgumentParser(
        description="Launch the registered coding-delivery pytest recipe on Linux.")
    parser.add_argument("--bwrap", help="Absolute installed bubblewrap executable (default: PATH)")
    if argv == ["--help"] or argv == ["-h"]:
        parser.parse_args(argv)
    try:
        boundary = argv.index("--")
    except ValueError as exc:
        raise ValueError("sandbox command delimiter is missing") from exc
    options = parser.parse_args(argv[:boundary])
    bwrap = options.bwrap if options.bwrap is not None else shutil.which("bwrap")
    if not bwrap or not Path(bwrap).is_absolute():
        raise ValueError("absolute installed bubblewrap executable is required")
    source = Path.cwd().resolve(strict=True)
    return ["--bwrap", bwrap, "--base", str(Path(sys.base_prefix).resolve(strict=True)),
        "--venv", str(Path(sys.prefix).resolve(strict=True)),
        "--python", str(Path(sys.executable).absolute()), "--mode", "test",
        "--source", str(source), "--junit", str(source / ".fleet-workflow-results.xml"),
        "--", *argv[boundary + 1:]]


def _arguments(argv: list[str]) -> tuple[dict[str, str], list[str]]:
    try:
        boundary = argv.index("--")
    except ValueError as exc:
        raise ValueError("sandbox command delimiter is missing") from exc
    options = argv[:boundary]
    command = argv[boundary + 1:]
    expected = {"--bwrap", "--base", "--venv", "--python", "--mode", "--source"}
    parsed: dict[str, str] = {}
    index = 0
    while index < len(options):
        key = options[index]
        if key not in expected | {"--junit", "--control", "--scratch", "--reviewer"}:
            raise ValueError("unknown sandbox option")
        if key in parsed or index + 1 == len(options):
            raise ValueError("duplicate or incomplete sandbox option")
        parsed[key] = options[index + 1]
        index += 2
    mode = parsed.get("--mode")
    required = expected | ({"--junit"} if mode == "test" else {"--control", "--scratch", "--reviewer"})
    if mode not in {"test", "review"} or set(parsed) != required or not command:
        raise ValueError("invalid sandbox mode arguments")
    return parsed, command


def _directory(options: dict[str, str], key: str) -> Path:
    path = Path(options[key]).resolve(strict=True)
    if not path.is_dir():
        raise ValueError("sandbox directory is invalid: " + key)
    return path


def _seccomp_fd() -> int:
    library = ctypes.util.find_library("seccomp")
    if not library:
        raise RuntimeError("Linux workflow isolation requires libseccomp")
    seccomp = ctypes.CDLL(library, use_errno=True)
    seccomp.seccomp_init.argtypes = [ctypes.c_uint32]
    seccomp.seccomp_init.restype = ctypes.c_void_p
    seccomp.seccomp_release.argtypes = [ctypes.c_void_p]
    seccomp.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    seccomp.seccomp_syscall_resolve_name.restype = ctypes.c_int
    seccomp.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    seccomp.seccomp_rule_add.restype = ctypes.c_int
    seccomp.seccomp_export_bpf.argtypes = [ctypes.c_void_p, ctypes.c_int]
    seccomp.seccomp_export_bpf.restype = ctypes.c_int

    context = seccomp.seccomp_init(0x7FFF0000)
    if not context:
        raise OSError(ctypes.get_errno() or errno.ENOMEM, "seccomp_init failed")
    descriptor = -1
    try:
        for name in _NETWORK_SYSCALLS:
            number = seccomp.seccomp_syscall_resolve_name(name.encode("ascii"))
            if number < 0:
                raise RuntimeError("required seccomp syscall is unavailable: " + name)
            result = seccomp.seccomp_rule_add(context, 0x00050000 | errno.EPERM, number, 0)
            if result < 0:
                raise OSError(-result, "seccomp rule failed: " + name)
        descriptor = os.memfd_create("fleet-workflow-seccomp", flags=0)
        result = seccomp.seccomp_export_bpf(context, descriptor)
        if result < 0:
            raise OSError(-result, "seccomp BPF export failed")
        size = os.fstat(descriptor).st_size
        if size < 8 or size > _BPF_LIMIT:
            raise RuntimeError("seccomp BPF size is invalid")
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.set_inheritable(descriptor, True)
        return descriptor
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    finally:
        seccomp.seccomp_release(context)


def _validate_command(mode: str, command: list[str], options: dict[str, str]) -> None:
    if Path(command[0]).resolve(strict=True) != Path(sys.executable).resolve(strict=True):
        raise ValueError("command does not use the registered Python runtime")
    if mode == "test":
        if command[1:] != ["-m", "pytest", "-q", "--junitxml=.fleet-workflow-results.xml"]:
            raise ValueError("fixed-test command does not match the registered recipe")
        return
    reviewer = str(Path(options["--reviewer"]).resolve(strict=True))
    if command[1:] not in (["-I", reviewer], [reviewer]):
        raise ValueError("review command does not match the registered script")


def _mounts(options: dict[str, str], mode: str) -> list[str]:
    bwrap = Path(options["--bwrap"]).resolve(strict=True)
    base = _directory(options, "--base")
    venv = _directory(options, "--venv")
    source = _directory(options, "--source")
    mounts = [
        "--unshare-user", "--unshare-pid", "--unshare-net", "--die-with-parent",
        "--uid", "0", "--gid", "0", "--cap-drop", "ALL", "--proc", "/proc", "--dev", "/dev",
        "--tmpfs", "/tmp",
    ]
    seen: set[Path] = set()
    for path in sorted({base, venv}, key=lambda item: (len(item.parts), str(item))):
        if path not in seen:
            mounts.extend(("--ro-bind", str(path), str(path)))
            seen.add(path)
    for system_path in (Path("/lib"), Path("/lib64"), Path("/usr/lib"), Path("/usr/lib64")):
        if system_path.exists() and system_path not in seen:
            mounts.extend(("--ro-bind", str(system_path), str(system_path)))
            seen.add(system_path)
    python = Path(options["--python"])
    if not python.is_absolute() or python.resolve(strict=True) != Path(sys.executable).resolve(strict=True):
        raise ValueError("registered Python executable is invalid")
    # A venv may target an alias outside the resolved base prefix (for example uv).
    # Mount only that same runtime at its alias; the surrounding host tree stays hidden.
    if python.is_symlink():
        target = python
        for _ in range(40):
            if not target.is_symlink():
                break
            target = target.parent / target.readlink()
        else:
            raise ValueError("Python executable symlink chain is too deep")
        alias = Path(os.path.normpath(str(target.parent.parent)))
        if alias.resolve(strict=True) != base:
            raise ValueError("Python executable alias is outside its runtime")
        if alias != base:
            mounts.extend(("--ro-bind", str(base), str(alias)))
    mounts.extend(("--ro-bind", str(source), str(source)))
    # The tiny exec stage applies the process budget only after bubblewrap has
    # established a new user namespace, independent of the shared host uid.
    script = Path(__file__).resolve(strict=True)
    mounts.extend(("--ro-bind", str(script), str(script)))
    if mode == "test":
        writable = [source / ".sandbox-home", source / ".sandbox-tmp", source / ".pytest_cache"]
        junit = Path(options["--junit"]).resolve()
        if junit != source / ".fleet-workflow-results.xml":
            raise ValueError("JUnit path escaped the fixed-test work directory")
        if junit.exists() or junit.is_symlink():
            raise ValueError("JUnit path is not fresh")
        (source / ".pytest_cache").mkdir(mode=0o700, exist_ok=True)
        for path in writable:
            if not path.is_dir() or path.is_symlink():
                raise ValueError("fixed-test writable path is invalid")
            mounts.extend(("--bind", str(path), str(path)))
        descriptor = os.open(junit, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(descriptor)
        mounts.extend(("--bind", str(junit), str(junit)))
    else:
        control = _directory(options, "--control")
        scratch = _directory(options, "--scratch")
        reviewer = Path(options["--reviewer"]).resolve(strict=True)
        if not reviewer.is_file() or reviewer.is_symlink():
            raise ValueError("reviewer script must be a regular file")
        request = control / "request.json"
        if not request.is_file() or request.is_symlink():
            raise ValueError("review request must be a regular file")
        if not scratch.is_dir() or scratch.is_symlink():
            raise ValueError("review scratch directory is invalid")
        mounts.extend(("--ro-bind", str(control), str(control), "--ro-bind", str(reviewer), str(reviewer),
                       "--bind", str(scratch), str(scratch)))
    mounts.extend(("--remount-ro", "/"))
    mounts.extend(("--remount-ro", "/tmp"))
    mounts.extend(("--chdir", str(source)))
    return [str(bwrap), *mounts]


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments[:1] == ["--namespace-exec"]:
        import resource

        command = arguments[1:]
        if (not command or not Path(command[0]).is_absolute()
                or Path(command[0]).resolve(strict=True) != Path(sys.executable).resolve(strict=True)):
            raise ValueError("namespace command must use the registered Python")
        resource.setrlimit(resource.RLIMIT_NPROC, (32, 32))
        os.execv(command[0], command)
        return 0
    if arguments[:1] == ["--fixed-test"]:
        arguments = _fixed_test_arguments(arguments[1:])
    options, command = _arguments(arguments)
    mode = options["--mode"]
    if not Path(options["--bwrap"]).is_file() or not os.access(options["--bwrap"], os.X_OK):
        raise ValueError("bubblewrap executable is invalid")
    _validate_command(mode, command, options)
    descriptor = _seccomp_fd()
    try:
        environment = {
            "PATH": str(Path(sys.prefix) / "bin") + os.pathsep + "/usr/bin:/bin",
            "HOME": str(Path(options["--source"]) / ".sandbox-home") if mode == "test" else options["--scratch"],
            "TMPDIR": str(Path(options["--source"]) / ".sandbox-tmp") if mode == "test" else options["--scratch"],
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        }
        if mode == "review":
            environment.update({"FLEET_REVIEW_SOURCE": options["--source"],
                                "FLEET_REVIEW_REQUEST": str(Path(options["--control"]) / "request.json")})
        bwrap = _mounts(options, mode)
        args = [*bwrap, "--seccomp", str(descriptor), "--clearenv"]
        for key, value in environment.items():
            args.extend(("--setenv", key, value))
        args.extend(("--", options["--python"], "-I", str(Path(__file__).resolve()),
                     "--namespace-exec", *command))
        os.execv(options["--bwrap"], args)
    finally:
        os.close(descriptor)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
