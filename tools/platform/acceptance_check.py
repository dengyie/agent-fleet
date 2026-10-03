"""Account-authenticated release acceptance: one read-only Run per selected model.

This deliberately creates labeled conversations and consumes model requests.
It never retries a submission or replays an unknown Run. Reports exclude
credentials, prompts, response bodies and file listings.
"""
import argparse
import http.client
import json
from pathlib import Path
import secrets
import socket
from threading import Timer
import time
from urllib.parse import urlsplit, quote

MAX_BYTES = 2 * 1024 * 1024


class AcceptanceFailure(Exception):
    def __init__(self, code, status=None):
        self.code, self.status = code, status


def remaining(deadline):
    budget = deadline - time.monotonic()
    if budget <= 0:
        raise AcceptanceFailure('acceptance_timeout')
    return budget


class AccountClient:
    def __init__(self, endpoint):
        self.endpoint = endpoint.rstrip('/')
        self.cookie = None

    def request(self, path, body=None, *, expected=200, deadline=None):
        budget = min(15.0, remaining(deadline)) if deadline is not None else 15.0
        request_deadline = time.monotonic() + budget
        headers = {'User-Agent': 'agent-fleet/1.0', 'Origin': self.endpoint, 'Content-Type': 'application/json'}
        if self.cookie:
            headers['Cookie'] = self.cookie
        origin = urlsplit(self.endpoint)
        connection_type = http.client.HTTPSConnection if origin.scheme == 'https' else http.client.HTTPConnection
        connection = connection_type(origin.hostname, origin.port, timeout=budget)
        timer = None
        try:
            connection.connect()
            if deadline is not None:
                remaining(deadline)
            request_budget = request_deadline - time.monotonic()
            if request_budget <= 0:
                raise AcceptanceFailure('transport_unknown')
            sock = connection.sock
            sock.settimeout(request_budget)

            def interrupt():
                # Socket timeouts reset after successful reads. Shutdown also
                # bounds peers that continuously trickle headers/body bytes.
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

            timer = Timer(request_budget, interrupt)
            timer.daemon = True
            timer.start()
            connection.request('GET' if body is None else 'POST', path,
                               body=None if body is None else json.dumps(body).encode(), headers=headers)
            response = connection.getresponse()  # Never follow redirects or replay requests.
            with response:
                raw = response.read(MAX_BYTES + 1)
                if deadline is not None:
                    remaining(deadline)
                if response.status != expected:
                    raise AcceptanceFailure('unexpected_http_status', response.status)
                if len(raw) > MAX_BYTES:
                    raise AcceptanceFailure('response_too_large')
                cookie = response.headers.get('Set-Cookie')
                if cookie:
                    self.cookie = cookie.split(';', 1)[0]
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    raise ValueError()
                return payload
        except (OSError, http.client.HTTPException):
            if deadline is not None:
                remaining(deadline)
            raise AcceptanceFailure('transport_unknown') from None
        except (ValueError, UnicodeError):
            raise AcceptanceFailure('invalid_response') from None
        finally:
            if timer is not None:
                timer.cancel()
            connection.close()


def check_model(client, model, workspace, timeout):
    row = {'model_profile_id': model, 'status': 'failed'}
    deadline = time.monotonic() + timeout

    def request(path, body=None, *, expected=200):
        remaining(deadline)
        payload = client.request(path, body, expected=expected, deadline=deadline)
        remaining(deadline)
        return payload

    try:
        conversation = request('/api/platform/v1/conversations', {
            'title': '发布验收：模型与只读工作区调用', 'workspace_id': workspace,
        })['conversation']['conversation_id']
        row['conversation_id'] = conversation
        row['client_token'] = 'acceptance-' + secrets.token_hex(12)
        turn = request('/api/platform/v1/conversations/' + quote(conversation, safe='') + '/acceptance-turns', {
            'text': '请使用工作区列表工具检查当前工作区顶层内容，根据工具的真实返回简要说明有哪些文件和目录。只读检查，不执行命令，不创建或修改文件，不读取任何凭据。',
            'client_token': row['client_token'],
            'overrides': {'model_profile_id': model},
        }, expected=202)
        run_id = turn['run']['run_id']; row['run_id'] = run_id
        while True:
            run = request('/api/platform/v1/runs/' + quote(run_id, safe=''))
            state = run['state']; row['run_state'] = state
            if state in ('succeeded', 'failed', 'cancelled', 'unknown'):
                break
            time.sleep(min(1.0, remaining(deadline)))
        if state != 'succeeded':
            raise AcceptanceFailure('run_' + state)
        if not isinstance(run.get('result_text'), str) or not run['result_text'].strip():
            raise AcceptanceFailure('missing_final_reply')
        events = request('/api/platform/v1/runs/' + quote(run_id, safe='') + '/events')['events']
        results = [e['payload'] for e in events if e['kind'] == 'tool_result']
        if not results or any(p.get('tool') != 'workspace.list' or p.get('state') != 'succeeded' for p in results):
            raise AcceptanceFailure('read_only_tool_verification_failed')
        recovered = request('/api/platform/v1/conversations/' + quote(conversation, safe=''))['conversation']
        if not any(r['run_id'] == run_id and r['state'] == 'succeeded' and r['result_text'] == run['result_text'] for r in recovered['runs']):
            raise AcceptanceFailure('conversation_recovery_failed')
        remaining(deadline)
        row.update(status='passed', reply_present=True, read_only_tool_succeeded=True, recovered=True)
    except AcceptanceFailure as exc:
        row.update(error=exc.code, http_status=exc.status)
        if exc.code in ('acceptance_timeout', 'transport_unknown'):
            row['outcome'] = 'unconfirmed'
    except (KeyError, TypeError):
        row['error'] = 'invalid_response_contract'
    return row


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--endpoint', required=True)
    parser.add_argument('--credentials-file', required=True, help='Private JSON with username/email and password; never a CLI password')
    parser.add_argument('--workspace', required=True)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument('--model', action='append', dest='models')
    selection.add_argument('--all-models', action='store_true')
    parser.add_argument('--timeout', type=int, default=150)
    parser.add_argument('--report')
    parser.add_argument('--expected-release', help='Require the served frontend manifest version to equal this release')
    args = parser.parse_args(argv)
    try:
        url = urlsplit(args.endpoint)
        valid = (url.scheme in ('http', 'https') and url.hostname and not url.username and not url.password
                 and url.path in ('', '/') and not url.query and not url.fragment
                 and (url.scheme == 'https' or url.hostname in ('localhost', '127.0.0.1', '::1')))
        url.port
    except ValueError:
        valid = False
    if not valid:
        parser.error('endpoint must be an HTTPS origin (HTTP permitted only on loopback) without credentials')
    if not 1 <= args.timeout <= 600:
        parser.error('timeout must be 1..600 seconds per model')
    report = {'schema_version': 1, 'status': 'failed', 'models': [],
              'checks': {}, 'external_mail': 'not_tested', 'remote_execution': 'not_tested'}
    client = AccountClient(args.endpoint)
    authenticated = False
    try:
        if args.expected_release:
            manifest = client.request('/manifest.json')
            if manifest.get('version') != args.expected_release:
                raise AcceptanceFailure('release_version_mismatch')
            report['release'] = args.expected_release
        credentials_path = Path(args.credentials_file)
        if credentials_path.stat().st_mode & 0o077:
            raise AcceptanceFailure('credentials_file_not_private')
        credentials = json.loads(credentials_path.read_text())
        if not isinstance(credentials, dict):
            raise AcceptanceFailure('invalid_credentials_file')
        login = credentials.get('username') or credentials.get('email')
        password = credentials.get('password')
        if not isinstance(login, str) or not isinstance(password, str) or not login or not password:
            raise AcceptanceFailure('invalid_credentials_file')
        client.request('/api/accounts/login', {'email': login, 'password': password})
        authenticated = bool(client.cookie)
        if not authenticated:
            raise AcceptanceFailure('login_cookie_missing')
        client.request('/api/operator/session'); report['checks']['account_session'] = 'passed'
        readiness = client.request('/api/platform/v1/readiness')
        if readiness.get('configuration_ready') is not True:
            raise AcceptanceFailure('configuration_not_ready')
        report['checks']['configuration'] = 'passed'
        catalog = client.request('/api/platform/v1/defaults')
        available = {m['profile_id'] for m in catalog['models'] if m.get('enabled', True)}
        selected = sorted(available) if args.all_models else list(dict.fromkeys(args.models))
        if not selected or not set(selected) <= available:
            raise AcceptanceFailure('selected_model_unavailable')
        if args.workspace not in {w['workspace_id'] for w in catalog['workspaces'] if w.get('enabled', True)}:
            raise AcceptanceFailure('selected_workspace_unavailable')
        report['models'] = [check_model(client, model, args.workspace, args.timeout) for model in selected]
        report['status'] = 'passed' if all(row['status'] == 'passed' for row in report['models']) else 'failed'
    except AcceptanceFailure as exc:
        report.update(error=exc.code, http_status=exc.status)
    except (OSError, ValueError, KeyError, TypeError):
        report['error'] = 'invalid_configuration_or_response'
    finally:
        if authenticated:
            try:
                old_cookie = client.cookie
                client.request('/api/accounts/logout', {})
                # Check revocation of the old cookie, not just an anonymous request.
                client.cookie = old_cookie
                client.request('/api/operator/session', expected=401)
                report['checks']['logout'] = 'passed'
            except AcceptanceFailure:
                report['status'] = 'failed'; report['checks']['logout'] = 'failed'
    output = json.dumps(report, ensure_ascii=False, indent=2)
    print(output)
    if args.report:
        path = Path(args.report); path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(output + '\n')
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
