#!/usr/bin/env python3
"""Push-only control entrypoint.

State observation is supported through local probes. Remote command dispatch
is intentionally unavailable until an authenticated agent-side command queue
is implemented.
"""

import sys


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 2:
        print("用法: python3 hub/control.py <machine> <agent_type> <action>")
        return 1
    print("push-only 模式不支持中心向 agent 下发控制命令", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
