"""Compare publicly served frontend resources to this run's release artifact."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen


def verify_release(release: Path, origin: str, version: str) -> dict:
    manifest = json.loads((release / 'manifest.json').read_text(encoding='utf-8'))
    if manifest['version'] != version:
        raise ValueError('local_frontend_version_mismatch')
    # Use the URLs browsers really request. No random cache buster, force-refresh
    # header or origin bypass: those could hide a stale public cache entry.
    resources = [('/', 'index.html'), ('/manifest.json', 'manifest.json')]
    resources.extend(('/' + quote(name, safe='/') + '?v=' + quote(version, safe=''), name)
                     for name in manifest['files'])

    def verify(resource):
        url_path, name = resource
        expected = (release / name).read_bytes()
        request = Request(origin.rstrip('/') + url_path,
                          headers={'User-Agent': 'AgentFleet-ReleaseVerifier/1.0',
                                   'Accept': '*/*'})
        with urlopen(request, timeout=15) as response:
            actual = response.read(len(expected) + 1)
        if actual != expected:
            raise ValueError(
                f'public_frontend_mismatch:{url_path} '
                f'expected={hashlib.sha256(expected).hexdigest()} '
                f'actual={hashlib.sha256(actual).hexdigest()}')

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(verify, resources))
    return {'version': version, 'origin': origin, 'verified_resources': len(resources)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release', required=True, type=Path)
    parser.add_argument('--origin', required=True)
    parser.add_argument('--version', required=True)
    args = parser.parse_args()
    print(json.dumps(verify_release(args.release, args.origin, args.version)))
