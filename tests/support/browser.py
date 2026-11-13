"""Synthetic external-origin policy fixture for injected browser drivers."""
from __future__ import annotations

import socket

from tools.platform.browser_backend import LocalBrowserBackend


ORIGIN = "https://browser.fixture.test"
URL = ORIGIN + "/"
PUBLIC_TEST_ADDRESS = "93.184.216.34"


def resolve_origin(host: str, port: int, *, type: int):
    if host != "browser.fixture.test" or port != 443 or type != socket.SOCK_STREAM:
        raise socket.gaierror("unexpected browser fixture resolver input")
    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "",
             (PUBLIC_TEST_ADDRESS, port))]


def browser_backend(driver_factory, *, submit_enabled: bool = False) -> LocalBrowserBackend:
    return LocalBrowserBackend(
        driver_factory, network_enabled=True, allowed_origins=(ORIGIN,),
        resolver=resolve_origin, submit_enabled=submit_enabled,
    )
