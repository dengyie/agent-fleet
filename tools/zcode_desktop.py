"""Message an existing ZCode desktop session through its installed extension.

This client never resumes a CLI or mutates native conversation storage. A send
requires a stable caller-generated request ID. An unknown result must be queried,
not retried under a new ID. The descriptor stays local and is never logged.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import urllib.error
import urllib.parse
import urllib.request


DEFAULT_ENDPOINT = (Path.home() / '.zcode/cli/plugins/data'
                    / 'agent-fleet-bridge@agent-fleet/bridge.json')


class BridgeError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ZCodeDesktopClient:
    def __init__(self, endpoint: Path = DEFAULT_ENDPOINT, timeout: float = 30):
        self.endpoint = Path(endpoint)
        self.timeout = timeout
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def request(self, path: str, payload: dict | None = None) -> dict:
        try:
            with self.endpoint.open('r', encoding='utf-8') as stream:
                metadata = os.fstat(stream.fileno())
                if os.name != 'nt' and (metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077):
                    raise BridgeError('endpoint_permissions_invalid')
                config = json.load(stream)
        except (OSError, ValueError) as exc:
            raise BridgeError('desktop_bridge_unavailable') from exc
        url = urllib.parse.urlsplit(config.get('url', ''))
        try:
            valid = (config.get('active') is True and config.get('protocol') == 1
                     and url.scheme == 'http' and url.hostname == '127.0.0.1'
                     and url.port and not url.username and not url.password
                     and not url.path and not url.query and not url.fragment
                     and isinstance(config.get('token'), str) and len(config['token']) == 64)
        except ValueError:
            valid = False
        if not valid or not path.startswith('/') or path.startswith('//'):
            raise BridgeError('desktop_bridge_unavailable')
        request = urllib.request.Request(config['url'] + path,
            data=None if payload is None else json.dumps(payload, ensure_ascii=False).encode('utf-8'),
            headers={'Authorization': 'Bearer ' + config['token'], 'Content-Type': 'application/json'})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                error = json.load(exc).get('error', 'bridge_request_failed')
            except (ValueError, AttributeError):
                error = 'bridge_request_failed'
            raise BridgeError(error) from None
        except (OSError, ValueError) as exc:
            raise BridgeError('delivery_unknown_query_original_request' if payload is not None else 'bridge_unavailable') from exc

    def send(self, session_id: str, request_id: str, text: str) -> dict:
        return self.request('/send', {'session_id': session_id, 'request_id': request_id, 'text': text})

    def receipt(self, request_id: str) -> dict:
        return self.request('/receipts/' + urllib.parse.quote(request_id, safe=''))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--endpoint', type=Path, default=DEFAULT_ENDPOINT)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('health', 'visible', 'sessions', 'request-grants'):
        sub.add_parser(name)
    send = sub.add_parser('send')
    send.add_argument('--session-id', required=True)
    send.add_argument('--request-id', required=True)
    send.add_argument('--text', required=True)
    receipt = sub.add_parser('receipt')
    receipt.add_argument('--request-id', required=True)
    messages = sub.add_parser('messages')
    messages.add_argument('--session-id', required=True)
    messages.add_argument('--cursor')
    args = parser.parse_args(argv)
    client = ZCodeDesktopClient(args.endpoint)
    try:
        if args.command == 'send':
            result = client.send(args.session_id, args.request_id, args.text)
        elif args.command == 'receipt':
            result = client.receipt(args.request_id)
        elif args.command == 'messages':
            query = {'session_id': args.session_id}
            if args.cursor is not None:
                query['cursor'] = args.cursor
            result = client.request('/messages?' + urllib.parse.urlencode(query))
        else:
            result = client.request('/' + args.command, {} if args.command == 'request-grants' else None)
    except BridgeError as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    if args.command == 'send' and result.get('status') != 'accepted':
        return 2 if result.get('status') == 'rejected' else 3
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
