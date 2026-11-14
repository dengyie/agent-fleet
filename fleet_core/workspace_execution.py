"""Portable operator-owned workspace Node enrollment contract."""
from pathlib import PurePosixPath, PureWindowsPath
from urllib.parse import urlsplit


def valid_hub_origin(value):
    if type(value) is not str or len(value) > 2048 or any(ord(char) < 33 for char in value):
        raise ValueError('hub_https_origin_required')
    url = urlsplit(value)
    if (not url.hostname or (url.scheme != 'https' and not (
            url.scheme == 'http' and url.hostname in {'localhost', '127.0.0.1', '::1'}))
            or url.username or url.password or url.query or url.fragment or url.path not in {'', '/'}):
        raise ValueError('hub_https_origin_required')
    if url.port == 0:
        raise ValueError('hub_https_origin_required')
    return value.rstrip('/')


def validate_execution_mode(mode):
    if type(mode) is not str or mode not in {'sandbox', 'vm_direct'}:
        raise ValueError('invalid execution mode')
    return mode


def parse_workspace_execution(value):
    if type(value) is not dict or set(value) != {'execution_mode', 'roots', 'sandbox_launcher'}:
        raise ValueError('invalid workspace execution configuration')
    mode = validate_execution_mode(value['execution_mode'])
    roots, launcher = value['roots'], value['sandbox_launcher']
    if (type(roots) is not dict or not 1 <= len(roots) <= 32
            or any(type(key) is not str or not key or len(key) > 128
                   or any(ord(char) < 32 for char in key)
                   or type(path) is not str or len(path) > 4096 or '\x00' in path
                   or not (PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute())
                   for key, path in roots.items())):
        raise ValueError('absolute workspace roots required')
    if (type(launcher) is not list or len(launcher) > 64
            or any(type(arg) is not str or not arg or len(arg) > 4096 or '\x00' in arg for arg in launcher)
            or mode == 'vm_direct' and launcher
            or mode == 'sandbox' and not launcher):
        raise ValueError('invalid workspace sandbox launcher')
    return {'execution_mode': mode, 'roots': dict(roots), 'sandbox_launcher': list(launcher)}
