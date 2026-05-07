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
