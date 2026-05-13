"""Source providers for Blueprints Updater."""

import hashlib
import html
from abc import ABC, abstractmethod
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlparse, urlunparse

import orjson
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import slugify
from homeassistant.util import yaml as yaml_util

from .const import (
    DOMAIN_BITBUCKET,
    DOMAIN_CODEBERG,
    DOMAIN_GIST,
    DOMAIN_GITHUB,
    DOMAIN_GITHUB_RAW,
    DOMAIN_GITLAB,
    DOMAIN_HA_FORUM,
    DOMAIN_JSDELIVR,
    RE_FORUM_CODE_BLOCK,
    RE_FORUM_TOPIC_ID,
    RE_GIST_RAW,
    SourceProviderType,
)


def _normalize_hostname(hostname: str | None) -> str:
    """Normalize hostname for comparison (lowercase and strip 'www.')."""
    if not hostname:
        return ""
    hostname = hostname.lower()
    return hostname[4:] if hostname.startswith("www.") else hostname


def _replace_path_segment(url: str, raw_marker: str, from_seg: str, to_seg: str) -> str:
    """Helper to replace a specific path segment for raw URL normalization."""
    parsed = urlparse(url)
    path_parts = parsed.path.strip("/").split("/")
    raw_parts = [p.lower() for p in raw_marker.strip("/").split("/")]
    from_parts = [p.lower() for p in from_seg.strip("/").split("/")]
    to_parts = to_seg.strip("/").split("/")

    raw_len = len(raw_parts)
    from_len = len(from_parts)

    if not path_parts or path_parts == [""]:
        return url

    for i in range(2, len(path_parts)):
        current_parts_lower = [p.lower() for p in path_parts[i:]]
        if i + raw_len <= len(path_parts) and current_parts_lower[:raw_len] == raw_parts:
            return url
        if i + from_len <= len(path_parts) and current_parts_lower[:from_len] == from_parts:
            new_parts = path_parts[:i] + to_parts + path_parts[i + from_len :]
            return urlunparse(parsed._replace(path="/" + "/".join(new_parts)))

    return url


class SourceProvider(ABC):
    """Abstract base class for blueprint source providers."""

    @property
    @abstractmethod
    def provider_type(self) -> SourceProviderType:
        """Return the type of this provider."""

    @abstractmethod
    def can_handle(self, url: str) -> bool:
        """Check if this provider can handle the given URL."""

    @abstractmethod
    def normalize_url(self, url: str) -> str:
        """Normalize the URL for content fetching."""

    @abstractmethod
    def get_metadata(self, url: str, content: str | None = None) -> dict[str, str]:
        """Extract metadata (author, name) from URL or content."""

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

    @property
    def provider_type(self) -> SourceProviderType:
        """Return the type of this provider."""
        return SourceProviderType.GITHUB

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

        path_parts = parsed.path.strip("/").split("/")
        if len(path_parts) < 5:
            return url

        route_segment = path_parts[2].lower()
        if route_segment not in ("blob", "raw"):
            return url

        new_parts = [*path_parts[:2], *path_parts[3:]]

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

    def get_metadata(self, url: str, content: str | None = None) -> dict[str, str]:
        """Extract metadata from GitHub URL following HA Core parity (author/name)."""
        parsed = urlparse(url)
        path_parts = parsed.path.strip("/").split("/")
        author = path_parts[0] if len(path_parts) > 0 else "unknown"
        filename = path_parts[-1] if len(path_parts) > 0 else "blueprint.yaml"
        name = (
            filename[:-5]
            if filename.lower().endswith(".yaml")
            else (filename[:-4] if filename.lower().endswith(".yml") else filename)
        )
        return {"author": author, "name": name}

    def get_cdn_url(self, url: str) -> str | None:
        """Get jsDelivr CDN URL for GitHub source."""
        parsed = urlparse(url)
        hostname = _normalize_hostname(parsed.hostname)
        path_parts = [p for p in parsed.path.split("/") if p]

        if hostname == DOMAIN_GITHUB_RAW:
            if len(path_parts) < 4:
                return None
            if (
                len(path_parts) >= 6
                and path_parts[2] == "refs"
                and path_parts[3] in ("heads", "tags")
            ):
                user, repo = path_parts[:2]
                branch = path_parts[4]
                path = "/".join(path_parts[5:])
            else:
                user, repo, branch = path_parts[:3]
                path = "/".join(path_parts[3:])
        elif hostname == DOMAIN_GITHUB:
            if len(path_parts) < 5:
                return None

            if path_parts[2].lower() not in ("blob", "raw"):
                return None
            user, repo = path_parts[:2]
            if (
                len(path_parts) >= 7
                and path_parts[3] == "refs"
                and path_parts[4] in ("heads", "tags")
            ):
                branch = path_parts[5]
                path = "/".join(path_parts[6:])
            else:
                branch = path_parts[3]
                path = "/".join(path_parts[4:])
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

    @property
    def provider_type(self) -> SourceProviderType:
        """Return the type of this provider."""
        return SourceProviderType.GIST

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

    def get_metadata(self, url: str, content: str | None = None) -> dict[str, str]:
        """Extract metadata from Gist URL following HA Core parity (author/name)."""
        parsed = urlparse(url)
        path_parts = parsed.path.strip("/").split("/")
        author = path_parts[0] if len(path_parts) > 0 else "unknown"
        filename = path_parts[-1] if len(path_parts) > 0 else "blueprint.yaml"
        if filename == "raw" and len(path_parts) > 1:
            filename = path_parts[-2]
        name = (
            filename[:-5]
            if filename.lower().endswith(".yaml")
            else (filename[:-4] if filename.lower().endswith(".yml") else filename)
        )
        return {"author": author, "name": name}


class HAForumProvider(SourceProvider):
    """Provider for Home Assistant Community Forum blueprints."""

    @property
    def provider_type(self) -> SourceProviderType:
        """Return the type of this provider."""
        return SourceProviderType.HA_FORUM

    def can_handle(self, url: str) -> bool:
        """Check if URL is an HA Forum URL."""
        parsed = urlparse(url)
        hostname = _normalize_hostname(parsed.hostname)
        return hostname == DOMAIN_HA_FORUM

    def normalize_url(self, url: str) -> str:
        """Normalize Forum URL to topic JSON endpoint."""
        parsed = urlparse(url)

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

    def get_metadata(self, url: str, content: str | None = None) -> dict[str, str]:
        """Extract metadata from Forum URL, prioritizing username/slug from topic JSON."""
        if content:
            try:
                data = orjson.loads(content)
                if isinstance(data, dict):
                    post_stream = data.get("post_stream")
                    posts = post_stream.get("posts", []) if isinstance(post_stream, dict) else []
                    target_post = posts[0] if posts and isinstance(posts, list) else None

                    for post in posts:
                        if not isinstance(post, dict):
                            continue
                        cooked = post.get("cooked", "")
                        if "blueprint:" in cooked:
                            target_post = post
                            break

                    if target_post and isinstance(target_post, dict):
                        username = target_post.get("username")
                        slug = data.get("slug")
                        if username and slug:
                            return {"author": username, "name": slug}
            except (orjson.JSONDecodeError, KeyError, TypeError):
                pass

        parsed = urlparse(url)
        hostname = parsed.hostname.lower() if parsed.hostname else DOMAIN_HA_FORUM
        match = RE_FORUM_TOPIC_ID.search(parsed.path)
        topic_id = match.group(1) if match else "topic"
        return {"author": hostname, "name": topic_id}

    def parse_content(
        self, response_text: str, response_json: dict[str, Any] | None = None
    ) -> str | None:
        """Extract YAML blueprint from Forum JSON response."""
        if not isinstance(response_json, dict):
            return None

        post_stream = response_json.get("post_stream")
        if not isinstance(post_stream, dict):
            return None

        posts = post_stream.get("posts")
        if not isinstance(posts, list):
            return None

        for post in posts:
            if not isinstance(post, dict):
                continue

            post_content = post.get("cooked")
            if not isinstance(post_content, str):
                continue

            code_blocks: list[str] = RE_FORUM_CODE_BLOCK.findall(post_content)
            for block in code_blocks:
                unquoted_block = html.unescape(block).strip()
                if "blueprint:" in unquoted_block:
                    return unquoted_block
        return None


class GitLabProvider(SourceProvider):
    """Provider for GitLab hosted blueprints."""

    @property
    def provider_type(self) -> SourceProviderType:
        """Return the type of this provider."""
        return SourceProviderType.GITLAB

    def can_handle(self, url: str) -> bool:
        """Check if URL is a GitLab URL."""
        parsed = urlparse(url)
        hostname = _normalize_hostname(parsed.hostname)
        return hostname == DOMAIN_GITLAB

    def normalize_url(self, url: str) -> str:
        """Normalize GitLab URL to raw endpoint."""
        return _replace_path_segment(url, "/-/raw/", "/-/blob/", "/-/raw/")

    def get_metadata(self, url: str, content: str | None = None) -> dict[str, str]:
        """Extract metadata from GitLab URL (Matching HA Generic Logic)."""
        parsed = urlparse(url)
        author = parsed.hostname.lower() if parsed.hostname else "imported"
        path_parts = parsed.path.strip("/").split("/")
        filename = path_parts[-1] if path_parts else "blueprint.yaml"
        name = (
            filename[:-5]
            if filename.lower().endswith(".yaml")
            else (filename[:-4] if filename.lower().endswith(".yml") else filename)
        )
        return {"author": author, "name": name}


class CodebergProvider(SourceProvider):
    """Provider for Codeberg hosted blueprints."""

    @property
    def provider_type(self) -> SourceProviderType:
        """Return the type of this provider."""
        return SourceProviderType.CODEBERG

    def can_handle(self, url: str) -> bool:
        """Check if URL is a Codeberg URL."""
        parsed = urlparse(url)
        hostname = _normalize_hostname(parsed.hostname)
        return hostname == DOMAIN_CODEBERG

    def normalize_url(self, url: str) -> str:
        """Normalize Codeberg URL to raw endpoint."""
        return _replace_path_segment(url, "/raw/", "src", "raw")

    def get_metadata(self, url: str, content: str | None = None) -> dict[str, str]:
        """Extract metadata from Codeberg URL (Matching HA Generic Logic)."""
        parsed = urlparse(url)
        author = parsed.hostname.lower() if parsed.hostname else "imported"
        path_parts = parsed.path.strip("/").split("/")
        filename = path_parts[-1] if path_parts else "blueprint.yaml"
        name = (
            filename[:-5]
            if filename.lower().endswith(".yaml")
            else (filename[:-4] if filename.lower().endswith(".yml") else filename)
        )
        return {"author": author, "name": name}


class BitbucketProvider(SourceProvider):
    """Provider for Bitbucket hosted blueprints."""

    @property
    def provider_type(self) -> SourceProviderType:
        """Return the type of this provider."""
        return SourceProviderType.BITBUCKET

    def can_handle(self, url: str) -> bool:
        """Check if URL is a Bitbucket URL."""
        parsed = urlparse(url)
        hostname = _normalize_hostname(parsed.hostname)
        return hostname == DOMAIN_BITBUCKET

    def normalize_url(self, url: str) -> str:
        """Normalize Bitbucket URL to raw endpoint."""
        return _replace_path_segment(url, "/raw/", "src", "raw")

    def get_metadata(self, url: str, content: str | None = None) -> dict[str, str]:
        """Extract metadata from Bitbucket URL (Matching HA Generic Logic)."""
        parsed = urlparse(url)
        author = parsed.hostname.lower() if parsed.hostname else "imported"
        path_parts = parsed.path.strip("/").split("/")
        filename = path_parts[-1] if path_parts else "blueprint.yaml"
        name = (
            filename[:-5]
            if filename.lower().endswith(".yaml")
            else (filename[:-4] if filename.lower().endswith(".yml") else filename)
        )
        return {"author": author, "name": name}


class GenericProvider(SourceProvider):
    """Fallback provider for generic blueprint URLs."""

    @property
    def provider_type(self) -> SourceProviderType:
        """Return the type of this provider."""
        return SourceProviderType.GENERIC

    def can_handle(self, url: str) -> bool:
        """Generic provider handles anything as a last resort."""
        parsed = urlparse(url)
        return bool(parsed.scheme and parsed.netloc)

    def normalize_url(self, url: str) -> str:
        """No normalization for generic URLs."""
        return url

    def get_metadata(self, url: str, content: str | None = None) -> dict[str, str]:
        """Extract metadata from generic URL (HA Core Parity with Smart Fallback)."""
        parsed = urlparse(url)
        author = parsed.hostname.lower() if parsed.hostname else "imported"
        path_parts = parsed.path.strip("/").split("/")
        last_part = path_parts[-1] if path_parts else ""

        if last_part.lower().endswith((".yaml", ".yml")):
            name = (
                last_part[:-5]
                if last_part.lower().endswith(".yaml")
                else (last_part[:-4] if last_part.lower().endswith(".yml") else last_part)
            )
        elif content:
            try:
                data = yaml_util.parse_yaml(content)
                name = ""
                if isinstance(data, dict):
                    bp = data.get("blueprint")
                    if isinstance(bp, dict):
                        name = slugify(bp.get("name", ""))
            except HomeAssistantError:
                name = ""
        else:
            name = ""

        if not name:
            short_sha = hashlib.sha256(url.encode()).hexdigest()[:7]
            name = f"blueprint_{short_sha}"

        return {"author": author, "name": name}


class ProviderRegistry:
    """Registry to manage and lookup source providers."""

    def __init__(self) -> None:
        """Initialize the registry with default providers."""
        self._providers: list[SourceProvider] = [
            GitHubProvider(),
            GistProvider(),
            HAForumProvider(),
            GitLabProvider(),
            CodebergProvider(),
            BitbucketProvider(),
            GenericProvider(),
        ]

    def __iter__(self) -> Iterator[SourceProvider]:
        """Iterate over registered providers."""
        return iter(self._providers)

    def get_provider(self, url: str) -> SourceProvider | None:
        """Get the appropriate provider for the given URL."""
        for provider in self._providers:
            if not isinstance(provider, GenericProvider) and provider.can_handle(url):
                return provider

        generic = next((p for p in self._providers if isinstance(p, GenericProvider)), None)
        if generic and generic.can_handle(url):
            return generic

        return None

    def normalize_url(self, url: str) -> str:
        """Find appropriate provider and normalize URL."""
        if provider := self.get_provider(url):
            return provider.normalize_url(url)
        return url


registry = ProviderRegistry()
