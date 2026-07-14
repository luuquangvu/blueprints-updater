"""Shared helpers for coordinator tests."""

import httpx

from custom_components.blueprints_updater.const import REQUEST_TIMEOUT


async def mock_bounded_response(
    session: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
) -> httpx.Response:
    """Delegate legacy HTTP mocks through the bounded-fetch boundary."""
    return await session.get(
        url,
        headers=headers,
        timeout=REQUEST_TIMEOUT,
        follow_redirects=False,
    )
