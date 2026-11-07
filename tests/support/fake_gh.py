"""Temporary executable GitHub CLI fixture; never touches real credentials."""
import os
import sys

import pytest


@pytest.fixture
def fake_gh(tmp_path):
    if os.name == 'nt':
        pytest.skip('POSIX executable fixture; pure contracts and node transport are portable')
    path = tmp_path / 'gh'
    path.write_text('#!' + sys.executable + '\n' + '''
import json, os, sys, time
from pathlib import Path
root = Path(os.environ['GH_CONFIG_DIR'])
root.mkdir(parents=True, exist_ok=True)
args = sys.argv[1:]
if args == ['--version']:
    print('gh version 2.88.0 (fixture)')
elif args[:3] == ['config', 'get', 'git_protocol']:
    print((root / 'git-protocol').read_text() if (root / 'git-protocol').exists() else 'https')
elif args[:2] == ['auth', 'status']:
    if (root / 'unsupported').exists():
        print('unknown flag: --json', file=sys.stderr); sys.exit(1)
    if (root / 'unreachable').exists():
        print('dial tcp: network is unreachable', file=sys.stderr); sys.exit(1)
    if (root / 'not-logged-in').exists():
        print('You are not logged into any GitHub hosts.', file=sys.stderr); sys.exit(1)
    if (root / 'malformed').exists():
        print('{"hosts": []}'); sys.exit(0)
    if (root / 'json-network-error').exists():
        print(json.dumps({'hosts': {'github.com': [{'state':'error','error':'dial tcp: network is unreachable','login':'alice','active':True}]}}))
    elif (root / 'invalid').exists():
        print(json.dumps({'hosts': {'github.com': [{'state':'error','error':'invalid token','login':'alice','active':True}]}}))
    else:
        user = (root / 'account').read_text() if (root / 'account').exists() else 'alice'
        removed = (root / 'logged-out').read_text() if (root / 'logged-out').exists() else ''
        if user == removed: user = 'alice'
        users = list(dict.fromkeys([user, 'bob']))
        print(json.dumps({'hosts': {'github.com': [
          {'state':'success','login':u,'active':u==user,'tokenSource':'keyring','token':'sensitive-value','scopes':'repo, read:org'}
          for u in users if u != removed]}}))
elif args[:2] == ['auth', 'switch']:
    (root / 'account').write_text(args[args.index('--user')+1])
elif args[:2] == ['auth', 'logout']:
    (root / 'logged-out').write_text(args[args.index('--user')+1])
elif args[:2] == ['auth', 'login']:
    (root / 'login-protocol').write_text(args[args.index('--git-protocol')+1])
    print('! First copy your one-time code: ABCD-1234', flush=True)
    print('Open this URL to continue in your web browser: https://github.com/login/device', flush=True)
    (root / 'login-started').write_text(str(os.getpid()))
    while not (root / 'authorize').exists():
        time.sleep(0.05)
    (root / 'account').write_text('new-user')
    print('Authentication complete.', flush=True)
elif args and args[0] == 'api':
    if (root / 'invalid-repo').exists():
        print('[]'); sys.exit(0)
    print(json.dumps({'full_name':'owner/repo','private':True,'permissions':{'pull':True,'push':True,'admin':False},'token':'do-not-export'}))
else:
    sys.exit(3)
''', encoding='utf-8')
    path.chmod(0o700)
    return path
