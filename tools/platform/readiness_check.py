"""Read-only deployment gate. No tasks or model calls; nonzero if configuration is incomplete."""
import argparse
import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--endpoint', required=True)
    args = parser.parse_args(argv)
    url = urlsplit(args.endpoint)
    if url.scheme not in {'http', 'https'} or not url.hostname or url.username or url.password or url.query or url.fragment:
        parser.error('endpoint must be an HTTP(S) origin or base path without credentials')
    headers = {'User-Agent': 'agent-fleet/1.0'}
    token = os.environ.get('AGENT_FLEET_OPERATOR_TOKEN')
    if token: headers['X-Access-Token'] = token
    request = Request(args.endpoint.rstrip('/') + '/api/platform/v1/readiness', headers=headers)
    try:
        try:
            response = build_opener(NoRedirect()).open(request, timeout=10)
        except HTTPError as error:
            response = error
        with response:
            status = response.code
            raw = response.read(65537)
        if len(raw) > 65536: raise ValueError('oversized response')
        data = json.loads(raw)
        ready = status == 200 and data.get('configuration_ready') is True
        # Never print an arbitrary response body (it may contain an upstream secret).
        print(json.dumps({'configuration_ready': ready, 'http_status': status,
                          'provider_connectivity': 'not_tested'}))
        return 0 if ready else 1
    except (URLError, OSError, ValueError, AttributeError):
        print(json.dumps({'configuration_ready': False, 'error': 'readiness_check_failed'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
