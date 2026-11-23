#!/usr/bin/env python3
"""Owned executable bridge for agy acp; never opens a browser or signs commands."""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

# Absolute script invocation must work from a task worktree and installed client.
if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fleet_core.process.local_process import popen as popen_local
from tools.providers.acp_mailbox import AcpMailbox, _atomic
from tools.providers.agy_runtime import AgyRuntime


def main(argv=None):
    # Python's redirected stdio may use a Windows locale codec. The worker's
    # public wire is UTF-8 independently of the vendor environment.
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='strict')
    parser = argparse.ArgumentParser()
    parser.add_argument('--vendor-command', required=True)
    parser.add_argument('--instruction', required=True)
    parser.add_argument('--resume-session')
    parser.add_argument('--control-dir')
    parser.add_argument('--request-timeout', type=float, default=90)
    parser.add_argument('--settle-gap', type=float, default=.5)
    parser.add_argument('--requested-model')
    parser.add_argument('--requested-mode')
    args = parser.parse_args(argv)
    try: command = json.loads(args.vendor_command)
    except (ValueError, RecursionError): return 64
    if (not isinstance(command, list) or not command or len(command) > 64
            or not all(isinstance(x, str) and x and len(x) <= 4096 for x in command)):
        return 64
    stop, output_lock = threading.Event(), threading.Lock()
    def stopped(_sig, _frame): stop.set()
    for sig in (signal.SIGTERM, signal.SIGINT): signal.signal(sig, stopped)
    def emit(row):
        if row.get('type') == 'fleet_acp_session' and not args.control_dir: return
        with output_lock:
            sys.stdout.write(json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n')
            sys.stdout.flush()
    root, status = None, 125
    process = None
    try:
        if args.control_dir:
            root = AcpMailbox.begin(args.control_dir, session_id=Path(args.control_dir).name)
        process = popen_local(command, cwd=os.getcwd(), env=dict(os.environ),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=0, close_fds=True)
        runtime = AgyRuntime(process, cwd=os.getcwd(), emit=emit,
            control_dir=root, should_abort=lambda: stop.is_set() or bool(root and AcpMailbox.cancelled(root)),
            request_timeout=max(.05, min(90, args.request_timeout)),
            settle_gap=max(.05, min(2, args.settle_gap)))
        status = runtime.run(args.instruction, args.resume_session,
            requested_model=args.requested_model, requested_mode=args.requested_mode)
    except (OSError, ValueError):
        emit({'type': 'fleet_acp_error', 'code': 'provider_unavailable'})
        status = 127
    except Exception:
        emit({'type': 'fleet_acp_error', 'code': 'runtime_failed'})
        status = 125
    finally:
        if root:
            try:
                owner = AcpMailbox._read(root / 'owner.json')
                if owner and owner.get('pid') == os.getpid():
                    _atomic(root / 'owner.json', {**owner, 'state': 'closed'})
            except (OSError, ValueError):
                emit({'type': 'fleet_acp_error', 'code': 'mailbox_close_failed'})
                status = 125
        if process is not None:
            try:
                try: process.stdin.close()
                except (OSError, ValueError): pass
                try: process.wait(timeout=.5)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try: process.wait(timeout=.5)
                    except subprocess.TimeoutExpired:
                        process.kill(); process.wait(timeout=1)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                emit({'type': 'fleet_acp_error', 'code': 'provider_cleanup_failed'})
                status = 125
    return status


if __name__ == '__main__':
    raise SystemExit(main())
