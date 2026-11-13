"""Explicit local status/submit/read commands; prompts arrive on stdin."""
import argparse
import json
import sys

from .muse import DEFAULT_STATE, MuseBridge, MuseBridgeError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', default=str(DEFAULT_STATE))
    parser.add_argument('--scope', default='local')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('status')
    sub.add_parser('submit').add_argument('task_id')
    sub.add_parser('read').add_argument('task_id')
    args = parser.parse_args(argv)
    bridge = MuseBridge(args.state)
    try:
        if args.command == 'status':
            result = bridge.status()
        elif args.command == 'submit':
            result = bridge.submit(args.task_id, sys.stdin.read(8193), scope=args.scope)
        else:
            result = bridge.read(args.task_id, scope=args.scope)
        print(json.dumps({'ok': True, 'result': result}, ensure_ascii=False))
        return 0
    except MuseBridgeError as exc:
        print(json.dumps({'ok': False, 'error': exc.code}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
