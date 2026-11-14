"""Enter an owned cgroup before exec; launched by CommandScope in isolated mode."""
import os
from pathlib import Path
import sys


def main():
    group, parent, *argv = sys.argv[1:]
    if os.getppid() != int(parent):
        return 126
    (Path(group) / 'cgroup.procs').write_text(str(os.getpid()), encoding='ascii')
    if os.getppid() != int(parent):
        return 126
    os.execvpe(argv[0], argv, os.environ)


if __name__ == '__main__':
    sys.exit(main())
