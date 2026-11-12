"""Cross-process barrier for artifact publication and consistent backup copies."""
from contextlib import contextmanager
import os
from pathlib import Path


@contextmanager
def artifact_snapshot_barrier(root: Path):
    """Keep this sibling lock file stable; unlinking it would split the lock.

    Every publication (and any future deletion/GC) must hold this barrier.
    Backups hold it from BEFORE the database snapshot through the last artifact
    copy. A writer may record a reference only after publication completes.
    """
    root = Path(root).expanduser().resolve()
    root.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(root.parent / ('.' + root.name + '.snapshot.lock'), flags, 0o600)
    try:
        if os.name == 'nt':
            import msvcrt
            if os.fstat(fd).st_size == 0:
                os.write(fd, b'\0')
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        # Closing the descriptor releases the kernel lock, including when a
        # snapshot or publication raises before it can finish.
        os.close(fd)


class ArtifactSlotError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _try_io_slot(directory: int, name: str) -> int | None:
    if os.name == "nt":
        from .artifact_windows import try_lock
        return try_lock(directory, name)
    import fcntl
    import stat
    descriptor = os.open(name, os.O_RDONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
            raise ArtifactSlotError("unsafe_transfer_slot")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return descriptor
    except BlockingIOError:
        os.close(descriptor)
        return None
    except BaseException:
        os.close(descriptor)
        raise


def _validate_io_slot(key: str, capacity: int) -> None:
    if os.name not in {"posix", "nt"} or (os.name == "posix" and not hasattr(os, "O_NOFOLLOW")):
        raise ArtifactSlotError("capability_unavailable")
    if (type(capacity) is not int or not 1 <= capacity <= 4 or not key or len(key) > 160
            or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for char in key)):
        raise ArtifactSlotError("invalid_transfer_slot")


def artifact_io_slot_count(directory: int, key: str, capacity: int) -> int:
    """Probe a fixed kernel slot set while the caller holds its admission fence."""
    _validate_io_slot(key, capacity)
    busy = 0
    try:
        for slot in range(capacity):
            descriptor = _try_io_slot(directory, "." + key + ".slot-" + str(slot))
            if descriptor is None:
                busy += 1
            else:
                os.close(descriptor)
        return busy
    except OSError as exc:
        raise ArtifactSlotError("transfer_slot_unavailable") from exc


@contextmanager
def artifact_io_slot(directory: int, key: str, capacity: int):
    """Bound active byte streams across processes; kernel close releases a slot."""
    _validate_io_slot(key, capacity)
    descriptor = None
    try:
        for slot in range(capacity):
            descriptor = _try_io_slot(directory, "." + key + ".slot-" + str(slot))
            if descriptor is not None:
                break
        if descriptor is None:
            raise ArtifactSlotError("transfer_concurrency_limit")
        yield
    except OSError as exc:
        raise ArtifactSlotError("transfer_slot_unavailable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
