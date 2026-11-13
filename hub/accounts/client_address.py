"""Resolve rate-limit identity only through explicitly trusted proxy networks."""
from ipaddress import ip_address, ip_network

from hub.http.errors import ApplicationError


def proxy_networks(values):
    if isinstance(values, str):
        values = [value.strip() for value in values.split(',') if value.strip()]
    if not isinstance(values, (list, tuple)):
        raise ValueError('account trusted proxies must be a list of IP addresses/CIDRs')
    return tuple(ip_network(value) for value in values)


def client_address(request, networks):
    def invalid():
        raise ApplicationError('invalid_client_address', '无法确认客户端地址，请检查代理配置', 400)

    def parse(value):
        try:
            return ip_address(value.strip())
        except (ValueError, AttributeError):
            invalid()

    def trusted(address):
        return any(address in network for network in networks)

    address = parse(request.remote_addr)
    # Untrusted clients cannot choose their identity with a forwarding header.
    if not trusted(address):
        return str(address)
    forwarded = request.headers.get('X-Forwarded-For', '')
    if not forwarded or len(forwarded) > 2048:
        invalid()
    chain = forwarded.split(',')
    if len(chain) > 16:
        invalid()
    for hop in reversed(chain):
        if not trusted(address):
            break
        address = parse(hop)
    if trusted(address):
        # Never silently pool all clients into the proxy's shared quota.
        invalid()
    return str(address)
