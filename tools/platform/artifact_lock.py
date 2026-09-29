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
