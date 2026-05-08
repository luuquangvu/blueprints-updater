"""Tests for DNS mocking logic in tests/conftest.py."""

import socket

import pytest


@pytest.mark.asyncio
async def test_mock_getaddrinfo_logic():
    """Verify that DNS mocking correctly routes requests.

    Requests to localhost or special-use TLDs should resolve to local IPs,
    while other requests should be forced to the dummy public IP (1.1.1.1)
    defined in tests/conftest.py for safety.
    """
    res = socket.getaddrinfo("example.com", 80)
    assert res[0][4][0] == "1.1.1.1"

    res = socket.getaddrinfo("localhost", 80)
    assert res[0][4][0] in ("127.0.0.1", "::1", "127.0.0.2")

    res = socket.getaddrinfo("test.home.arpa", 80)
    assert res[0][4][0] == "127.0.0.2"

    res = socket.getaddrinfo("127.0.0.1", 80)
    assert res[0][4][0] == "127.0.0.1"

    res = socket.getaddrinfo("8.8.8.8", 80)
    assert res[0][4][0] == "1.1.1.1"


def test_mock_getaddrinfo_af_unspec():
    """Verify that AF_UNSPEC returns both IPv4 and IPv6 results."""
    res = socket.getaddrinfo("example.com", 80, family=socket.AF_UNSPEC)
    families = [r[0] for r in res]
    assert socket.AF_INET in families
    assert socket.AF_INET6 in families

    ips = [r[4][0] for r in res]
    assert "1.1.1.1" in ips
    assert "2606:4700:4700::1111" in ips

    res_local = socket.getaddrinfo("localhost", 80, family=socket.AF_UNSPEC)
    local_ips = [r[4][0] for r in res_local]
    assert any(ip in ("127.0.0.1", "::1") for ip in local_ips)
