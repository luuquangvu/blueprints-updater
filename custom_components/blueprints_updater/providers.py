"""Source providers for Blueprints Updater."""

from __future__ import annotations

import contextlib
import html
from abc import ABC, abstractmethod
from typing import Any
from urllib.parse import urlparse, urlunparse

from .const import (
    DOMAIN_GIST,
    DOMAIN_GITHUB,
    DOMAIN_GITHUB_RAW,
    DOMAIN_HA_FORUM,
    DOMAIN_JSDELIVR,
    RE_FORUM_CODE_BLOCK,
    RE_FORUM_TOPIC_ID,
    RE_GIST_RAW,
    RE_GITHUB_BLOB,
)


def _normalize_hostname(hostname: str | None) -> str:
    """Normalize hostname for comparison (lowercase and strip 'www.')."""
    if not hostname:
        return ""
    hostname = hostname.lower()
    return hostname[4:] if hostname.startswith("www.") else hostname


class SourceProvider(ABC):
    """Abstract base class for blueprint source providers."""

    @abstractmethod
    def can_handle(self, url: str) -> bool:
        """Check if this provider can handle the given URL."""

    @abstractmethod
    def normalize_url(self, url: str) -> str:
        """Normalize the URL for content fetching."""

    def get_cdn_url(self, url: str) -> str | None:
        """Get CDN URL for the given source URL if supported."""
        return None

    def parse_content(
        self, response_text: str, response_json: dict[str, Any] | None = None
    ) -> str | None:
        """Parse the response content to extract the blueprint YAML."""
        return response_text


class GitHubProvider(SourceProvider):
    """Provider for GitHub hosted blueprints."""

    def can_handle(self, url: str) -> bool:
        """Check if URL is a GitHub URL."""
        parsed = urlparse(url)
        hostname = _normalize_hostname(parsed.hostname)
        return hostname in (DOMAIN_GITHUB, DOMAIN_GITHUB_RAW)

    def normalize_url(self, url: str) -> str:
        """Normalize GitHub URL to raw content endpoint."""
        parsed = urlparse(url)
        hostname = _normalize_hostname(parsed.hostname)
        if hostname != DOMAIN_GITHUB:
            return url

        if not RE_GITHUB_BLOB.search(parsed.path):
            return url

        path_parts = parsed.path.strip("/").split("/")
        new_parts = [p for p in path_parts if p != "blob"]

        return urlunparse(
            (
                parsed.scheme,
                DOMAIN_GITHUB_RAW,
                "/" + "/".join(new_parts),
                parsed.params,
                parsed.query,
                parsed.fragment,
            )
        )

    def get_cdn_url(self, url: str) -> str | None:
        """Get jsDelivr CDN URL for GitHub source."""
        parsed = urlparse(url)
        hostname = _normalize_hostname(parsed.hostname)
        path_parts = [p for p in parsed.path.split("/") if p]

        if hostname == DOMAIN_GITHUB_RAW:
            if len(path_parts) < 4:
                return None
            user, repo, branch = path_parts[:3]
            path = "/".join(path_parts[3:])
        elif hostname == DOMAIN_GITHUB:
            if len(path_parts) < 5:
                return None

            try:
                anchor_idx = next(
                    (idx for idx, part in enumerate(path_parts) if part in ("blob", "raw")),
                    -1,
                )
                if anchor_idx != 2:
                    return None

                user = path_parts[anchor_idx - 2]
                repo = path_parts[anchor_idx - 1]
                branch = path_parts[anchor_idx + 1]
                path = "/".join(path_parts[anchor_idx + 2 :])
            except (IndexError, ValueError):
                return None
        else:
            return None

        return urlunparse(
            (
                "https",
                DOMAIN_JSDELIVR,
                f"/gh/{user}/{repo}@{branch}/{path}",
                "",
                "",
                "",
            )
        )


class GistProvider(SourceProvider):
    """Provider for GitHub Gist hosted blueprints."""

    def can_handle(self, url: str) -> bool:
        """Check if URL is a Gist URL."""
        parsed = urlparse(url)
        hostname = _normalize_hostname(parsed.hostname)
        return hostname == DOMAIN_GIST

    def normalize_url(self, url: str) -> str:
        """Normalize Gist URL to raw endpoint."""
        parsed = urlparse(url)
        if RE_GIST_RAW.search(parsed.path):
            return url

        return urlunparse(
            (
                parsed.scheme,
                parsed.netloc,
                f"{parsed.path.rstrip('/')}/raw",
                parsed.params,
                parsed.query,
                parsed.fragment,
            )
        )


class HAForumProvider(SourceProvider):
    """Provider for Home Assistant Community Forum blueprints."""

    def can_handle(self, url: str) -> bool:
        """Check if URL is an HA Forum URL."""
        parsed = urlparse(url)
        hostname = _normalize_hostname(parsed.hostname)
        return hostname == DOMAIN_HA_FORUM

    def normalize_url(self, url: str) -> str:
        """Normalize Forum URL to topic JSON endpoint."""
        parsed = urlparse(url)
        if "/t/" not in parsed.path:
            return url

        match = RE_FORUM_TOPIC_ID.search(parsed.path)
        if not match:
            return url

        topic_id = match.group(1)
        return urlunparse(
            (
                parsed.scheme,
                parsed.netloc,
                f"/t/{topic_id}.json",
                parsed.params,
                "",
                parsed.fragment,
            )
        )

    def parse_content(
        self, response_text: str, response_json: dict[str, Any] | None = None
    ) -> str | None:
        """Extract YAML blueprint from Forum JSON response."""
        if not response_json:
            return None

        with contextlib.suppress(KeyError, IndexError):
            post_stream = response_json.get("post_stream", {})
            posts = post_stream.get("posts", [])
            if not posts:
                return None

            post_content = posts[0].get("cooked")
            if not isinstance(post_content, str):
                return None

            code_blocks: list[str] = RE_FORUM_CODE_BLOCK.findall(post_content)
            for block in code_blocks:
                unquoted_block = str(html.unescape(block).strip())
                if "blueprint:" in unquoted_block:
                    return unquoted_block
        return None


class ProviderRegistry:
    """Registry to manage and lookup source providers."""

    def __init__(self) -> None:
        """Initialize the registry with default providers."""
        self._providers: list[SourceProvider] = [
            GitHubProvider(),
            GistProvider(),
            HAForumProvider(),
        ]

    def get_provider(self, url: str) -> SourceProvider | None:
        """Get the appropriate provider for the given URL."""
        return next(
            (provider for provider in self._providers if provider.can_handle(url)),
            None,
        )


registry = ProviderRegistry()
