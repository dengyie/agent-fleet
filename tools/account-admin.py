#!/usr/bin/env python3
"""Offline first-administrator bootstrap; secrets are entered without echo."""
import argparse
import getpass
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hub.accounts.store import AccountStore
from hub.http.errors import ApplicationError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True, help='Hub application root')
    parser.add_argument('--email', required=True)
    parser.add_argument('--username', help='Optional username for password login')
    args = parser.parse_args()
    password = getpass.getpass('管理员密码（至少 12 字符）: ')
    if password != getpass.getpass('再次输入密码: '):
        parser.error('两次密码不一致')
    try:
        AccountStore(args.root / 'var/accounts/accounts.db').bootstrap_admin(args.email, password, login_name=args.username)
    except ApplicationError as exc:
        parser.error(exc.detail)
    print('管理员账号已创建。请使用账号或邮箱和密码登录。')


if __name__ == '__main__':
    main()
