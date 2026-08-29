"""Source providers for Blueprints Updater."""

import contextlib
import hashlib
import html
import logging
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping
from pathlib import Path
from urllib.parse import ParseResult, urlparse, urlunparse

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
    RE_FORUM_CODE_BLOCK,
    RE_FORUM_TOPIC_ID,
    RE_GIST_RAW,
    SourceProviderType,
)

_LOGGER = logging.getLogger(__name__)


def _normalize_hostname(hostname: str | None) -> str:
    """Normalize hostname for comparison (lowercase and strip 'www.')."""
    if not hostname:
        return ""
    hostname = hostname.lower()
    return hostname[4:] if hostname.startswith("www.") else hostname


def _without_fragment(url: str) -> str:
    """Return a URL without its client-side fragment."""
    return urlunparse(urlparse(url)._replace(fragment=""))


# Default ports that are implicit for a given scheme and must be omitted from
# canonical URLs so that urls with and without the explicit port compare equal.
_SCHEME_DEFAULT_PORTS: dict[str, int] = {"https": 443, "http": 80}


def _build_netloc(hostname: str | None, port: int | None) -> str:
    """Build a normalized netloc string with proper IPv6 bracket handling.

    urlparse strips brackets from IPv6 literal addresses, so naive
    reconstruction (f"{host}:{port}") produces invalid netloc strings such as
    '::1:8080'.  This helper re-brackets IPv6 addresses (detected by the
    presence of ':' in the normalized host) before appending the port.
    Credentials (userinfo) are stripped during canonicalization to prevent
    sensitive information from entering hashes or metadata.
    """
    host = _normalize_hostname(hostname)
    bracketed = f"[{host}]" if ":" in host else host
    return f"{bracketed}:{port}" if port is not None else bracketed


def _normalize_netloc(parsed: ParseResult) -> str:
    """Build a normalized netloc from parsed URL, omitting default scheme ports.

    Strips credentials, preserves non-default ports (such as 8443 or 0), and
    preserves IPv6 bracketed hosts. Omits default ports (443 for HTTPS, 80 for HTTP).
    Preserves raw netloc without credentials if the port or hostname is malformed or out of range.
    """
    try:
        port = parsed.port
        hostname = parsed.hostname
    except ValueError:
        raw_netloc = parsed.netloc
        if "@" in raw_netloc:
            _, _, raw_netloc = raw_netloc.rpartition("@")
        return raw_netloc

    if port is not None and _SCHEME_DEFAULT_PORTS.get(parsed.scheme.lower()) == port:
        port = None

    return _build_netloc(hostname, port)


def _replace_path_segment(url: str, raw_marker: str, from_seg: str, to_seg: str) -> str:
    """Helper to replace a specific path segment for raw URL normalization.

    Scans path segments from position 2 onward (after owner/repo). At each
    position the raw_marker is checked first; a match means the path is already
    normalized. Then from_seg is checked and replaced with to_seg when found.
    The returned URL has a normalized netloc (with default ports omitted and
    non-default ports preserved) and no fragment.
    The input URL scheme is preserved.

    Callers must ensure raw_marker and from_seg are structurally disjoint at
    any given position, otherwise a raw_marker match may shadow an unprocessed
    from_seg that happens to appear later in the path.
    """
    parsed = urlparse(_without_fragment(url))
    netloc = _normalize_netloc(parsed)
    path_parts = parsed.path.strip("/").split("/")
    raw_parts = [p.lower() for p in raw_marker.strip("/").split("/")]
    from_parts = [p.lower() for p in from_seg.strip("/").split("/")]
    to_parts = to_seg.strip("/").split("/")

    raw_len = len(raw_parts)
    from_len = len(from_parts)

    scheme = parsed.scheme.lower()
    if not path_parts or path_parts == [""]:
        return urlunparse(parsed._replace(scheme=scheme, netloc=netloc))

    if len(path_parts) < 3:
        return urlunparse(parsed._replace(scheme=scheme, netloc=netloc))

    path_parts_lower = [p.lower() for p in path_parts]

    for i in range(2, len(path_parts)):
        if i + raw_len <= len(path_parts) and path_parts_lower[i : i + raw_len] == raw_parts:
            return urlunparse(parsed._replace(scheme=scheme, netloc=netloc))
        if i + from_len <= len(path_parts) and path_parts_lower[i : i + from_len] == from_parts:
            new_parts = path_parts[:i] + to_parts + path_parts[i + from_len :]
            return urlunparse(
                parsed._replace(
                    scheme=scheme,
                    netloc=netloc,
                    path="/" + "/".join(new_parts),
                )
            )

    return urlunparse(parsed._replace(scheme=scheme, netloc=netloc))


def _strip_yaml_extension(filename: str) -> str:
    """Strip .yaml or .yml extension from a filename, preserving original case."""
    lower = filename.lower()
    if lower.endswith(".yaml"):
        return filename[:-5]
    return filename[:-4] if lower.endswith(".yml") else filename


def _default_url_metadata(url: str) -> dict[str, str]:
    """Extract default metadata (author, name) from a URL.

    Shared by GitLab, Codeberg, Bitbucket, and GenericProvider for
    consistent hostname-based author extraction and filename-based naming.
    """
    parsed = urlparse(url)
    author = parsed.hostname.lower() if parsed.hostname else "imported"
    path_parts = [p for p in parsed.path.strip("/").split("/") if p]
    filename = path_parts[-1] if path_parts else "blueprint.yaml"
    name = _strip_yaml_extension(filename)
    return {"author": author, "name": name}


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

    def parse_content(
        self, response_text: str, response_json: Mapping[str, object] | None = None
    ) -> str | None:
        """Parse the response content to extract the blueprint YAML."""
        return response_text

    def canonicalize_url(self, url: str) -> str:
        """Return a stable canonical representation of the URL.

        Normalizes scheme (lowercase), hostname (lowercase, strips leading 'www.'),
        omits default ports for the scheme (443 for HTTPS, 80 for HTTP) while
        preserving non-default ports (such as 8443 or 0), strips credentials, and
        removes trailing slashes from the path so that is_same_source comparisons
        are not confused by case, port, or www-prefix variants.
        """
        parsed = urlparse(url.strip())
        netloc = _normalize_netloc(parsed)
        return urlunparse(
            parsed._replace(
                scheme=parsed.scheme.lower(),
                netloc=netloc,
                path=parsed.path.rstrip("/"),
                fragment="",
            )
        )

    def is_same_source(self, url1: str, url2: str) -> bool:
        """Check if two URLs represent the exact same source resource."""
        return self.canonicalize_url(self.normalize_url(url1)) == self.canonicalize_url(
            self.normalize_url(url2)
        )


class GitHubProvider(SourceProvider):
    """Provider for GitHub hosted blueprints."""

    @property
    def provider_type(self) -> SourceProviderType:
        """Return the type of this provider."""
        return SourceProviderType.GITHUB

    def can_handle(self, url: str) -> bool:
        """Check if URL is a GitHub URL."""
        parsed = urlparse(_without_fragment(url))
        hostname = _normalize_hostname(parsed.hostname)
        return hostname in (DOMAIN_GITHUB, DOMAIN_GITHUB_RAW)

    def normalize_url(self, url: str) -> str:
        """Normalize GitHub URL to raw content endpoint."""
        parsed = urlparse(_without_fragment(url))
        hostname = _normalize_hostname(parsed.hostname)
        scheme = parsed.scheme.lower()
        if hostname != DOMAIN_GITHUB:
            return urlunparse(parsed._replace(scheme=scheme, netloc=_normalize_netloc(parsed)))

        path_parts = parsed.path.strip("/").split("/")
        if len(path_parts) < 5:
            return urlunparse(parsed._replace(scheme=scheme, netloc=_normalize_netloc(parsed)))

        route_segment = path_parts[2].lower()
        if route_segment not in ("blob", "raw"):
            return urlunparse(parsed._replace(scheme=scheme, netloc=_normalize_netloc(parsed)))

        new_parts = [*path_parts[:2], *path_parts[3:]]

        return urlunparse(
            (
                scheme,
                DOMAIN_GITHUB_RAW,
                "/" + "/".join(new_parts),
                parsed.params,
                parsed.query,
                "",
            )
        )

    def get_metadata(self, url: str, content: str | None = None) -> dict[str, str]:
        """Extract metadata from GitHub URL following HA Core parity (author/name)."""
        parsed = urlparse(url)
        path_parts = [p for p in parsed.path.strip("/").split("/") if p]
        author = path_parts[0] if path_parts else "unknown"
        filename = path_parts[-1] if path_parts else "blueprint.yaml"
        name = _strip_yaml_extension(filename)
        return {"author": author, "name": name}


class GistProvider(SourceProvider):
    """Provider for GitHub Gist hosted blueprints."""

    @property
    def provider_type(self) -> SourceProviderType:
        """Return the type of this provider."""
        return SourceProviderType.GIST

    def can_handle(self, url: str) -> bool:
        """Check if URL is a Gist URL."""
        parsed = urlparse(_without_fragment(url))
        hostname = _normalize_hostname(parsed.hostname)
        return hostname == DOMAIN_GIST

    def normalize_url(self, url: str) -> str:
        """Normalize Gist URL to raw endpoint."""
        parsed = urlparse(_without_fragment(url))
        netloc = _normalize_netloc(parsed)
        scheme = parsed.scheme.lower()
        if RE_GIST_RAW.search(parsed.path):
            return urlunparse(parsed._replace(scheme=scheme, netloc=netloc))
        return urlunparse(
            (
                scheme,
                netloc,
                f"{parsed.path.rstrip('/')}/raw",
                parsed.params,
                parsed.query,
                "",
            )
        )

    def get_metadata(self, url: str, content: str | None = None) -> dict[str, str]:
        """Extract metadata from Gist URL following HA Core parity (author/name)."""
        parsed = urlparse(url)
        path_parts = [p for p in parsed.path.strip("/").split("/") if p]
        author = path_parts[0] if path_parts else "unknown"
        filename = path_parts[-1] if path_parts else "blueprint.yaml"
        if filename == "raw" and len(path_parts) > 1:
            filename = path_parts[-2]
        name = _strip_yaml_extension(filename)
        return {"author": author, "name": name}


def _extract_blueprint_from_forum_post(post: object) -> str | None:
    """Extract and validate blueprint YAML block from a single forum post object."""
    if not isinstance(post, dict):
        return None
    post_content = post.get("cooked")
    if not isinstance(post_content, str):
        return None
    code_blocks: list[str] = RE_FORUM_CODE_BLOCK.findall(post_content)
    for block in code_blocks:
        if "blueprint:" not in block:
            continue
        unquoted_block = html.unescape(block).strip()
        if "blueprint:" not in unquoted_block:
            continue
        try:
            parsed = yaml_util.parse_yaml(unquoted_block)
            if isinstance(parsed, dict) and isinstance(parsed.get("blueprint"), dict):
                return unquoted_block
        except HomeAssistantError as err:
            _LOGGER.debug("Skipping forum code block, not a valid blueprint YAML mapping: %s", err)
    return None


class HAForumProvider(SourceProvider):
    """Provider for Home Assistant Community Forum blueprints."""

    @property
    def provider_type(self) -> SourceProviderType:
        """Return the type of this provider."""
        return SourceProviderType.HA_FORUM

    def can_handle(self, url: str) -> bool:
        """Check if URL is an HA Forum URL."""
        parsed = urlparse(_without_fragment(url))
        hostname = _normalize_hostname(parsed.hostname)
        return hostname == DOMAIN_HA_FORUM

    def normalize_url(self, url: str) -> str:
        """Normalize Forum URL to topic JSON endpoint."""
        parsed = urlparse(_without_fragment(url))
        scheme = parsed.scheme.lower() or "https"
        netloc = _normalize_netloc(parsed) or DOMAIN_HA_FORUM

        match = RE_FORUM_TOPIC_ID.search(parsed.path)
        if not match:
            return urlunparse(parsed._replace(scheme=scheme, netloc=netloc))

        topic_id = match.group(1)
        return urlunparse(
            (
                scheme,
                netloc,
                f"/t/{topic_id}.json",
                parsed.params,
                "",
                "",
            )
        )

    def canonicalize_url(self, url: str) -> str:
        """Canonicalize Forum URL to stable topic format without slugs."""
        parsed = urlparse(url.strip())
        if match := RE_FORUM_TOPIC_ID.search(parsed.path):
            topic_id = match.group(1)
            netloc = _normalize_netloc(parsed) or DOMAIN_HA_FORUM
            return urlunparse(
                (
                    (parsed.scheme or "https").lower(),
                    netloc,
                    f"/t/{topic_id}",
                    "",
                    "",
                    "",
                )
            )
        return super().canonicalize_url(url)

    def get_metadata(self, url: str, content: str | None = None) -> dict[str, str]:
        """Extract metadata from Forum URL, prioritizing username/slug from topic JSON."""
        if content:
            with contextlib.suppress(orjson.JSONDecodeError, KeyError, TypeError):
                data = orjson.loads(content)
                if isinstance(data, dict):
                    post_stream = data.get("post_stream")
                    posts = post_stream.get("posts", []) if isinstance(post_stream, dict) else []
                    target_post: dict[str, object] | None = None

                    for post in posts:
                        if not isinstance(post, dict):
                            continue
                        if _extract_blueprint_from_forum_post(post) is not None:
                            target_post = post
                            break

                    if target_post is None and posts and isinstance(posts[0], dict):
                        target_post = posts[0]

                    if target_post and isinstance(target_post, dict):
                        username = target_post.get("username")
                        slug = data.get("slug")
                        if (
                            isinstance(username, str)
                            and isinstance(slug, str)
                            and username
                            and slug
                        ):
                            return {"author": username, "name": slug}
        parsed = urlparse(url)
        hostname = parsed.hostname.lower() if parsed.hostname else DOMAIN_HA_FORUM
        match = RE_FORUM_TOPIC_ID.search(parsed.path)
        topic_id = match.group(1) if match else "topic"
        return {"author": hostname, "name": topic_id}

    def parse_content(
        self, response_text: str, response_json: Mapping[str, object] | None = None
    ) -> str | None:
        """Extract YAML blueprint from Forum JSON response."""
        if not isinstance(response_json, dict):
            return None

        post_stream = response_json.get("post_stream")
        if not isinstance(post_stream, dict):
            return None

        posts = post_stream.get("posts")
        if not isinstance(posts, list) or not posts:
            return None

        if result := _extract_blueprint_from_forum_post(posts[0]):
            return result

        for post in posts[1:]:
            if result := _extract_blueprint_from_forum_post(post):
                return result

        return None


class GitLabProvider(SourceProvider):
    """Provider for GitLab hosted blueprints."""

    @property
    def provider_type(self) -> SourceProviderType:
        """Return the type of this provider."""
        return SourceProviderType.GITLAB

    def can_handle(self, url: str) -> bool:
        """Check if URL is a GitLab URL."""
        parsed = urlparse(_without_fragment(url))
        hostname = _normalize_hostname(parsed.hostname)
        return hostname == DOMAIN_GITLAB

    def normalize_url(self, url: str) -> str:
        """Normalize GitLab URL to raw endpoint."""
        return _replace_path_segment(url, "/-/raw/", "/-/blob/", "/-/raw/")

    def get_metadata(self, url: str, content: str | None = None) -> dict[str, str]:
        """Extract metadata from GitLab URL (Matching HA Generic Logic)."""
        return _default_url_metadata(url)


class CodebergProvider(SourceProvider):
    """Provider for Codeberg hosted blueprints."""

    @property
    def provider_type(self) -> SourceProviderType:
        """Return the type of this provider."""
        return SourceProviderType.CODEBERG

    def can_handle(self, url: str) -> bool:
        """Check if URL is a Codeberg URL."""
        parsed = urlparse(_without_fragment(url))
        hostname = _normalize_hostname(parsed.hostname)
        return hostname == DOMAIN_CODEBERG

    def normalize_url(self, url: str) -> str:
        """Normalize Codeberg URL to raw endpoint."""
        return _replace_path_segment(url, "/raw/", "src", "raw")

    def get_metadata(self, url: str, content: str | None = None) -> dict[str, str]:
        """Extract metadata from Codeberg URL (Matching HA Generic Logic)."""
        return _default_url_metadata(url)


class BitbucketProvider(SourceProvider):
    """Provider for Bitbucket hosted blueprints."""

    @property
    def provider_type(self) -> SourceProviderType:
        """Return the type of this provider."""
        return SourceProviderType.BITBUCKET

    def can_handle(self, url: str) -> bool:
        """Check if URL is a Bitbucket URL."""
        parsed = urlparse(_without_fragment(url))
        hostname = _normalize_hostname(parsed.hostname)
        return hostname == DOMAIN_BITBUCKET

    def normalize_url(self, url: str) -> str:
        """Normalize Bitbucket URL to raw endpoint."""
        return _replace_path_segment(url, "/raw/", "src", "raw")

    def get_metadata(self, url: str, content: str | None = None) -> dict[str, str]:
        """Extract metadata from Bitbucket URL (Matching HA Generic Logic)."""
        return _default_url_metadata(url)


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
        """Return the generic URL without its client-side fragment."""
        parsed = urlparse(_without_fragment(url))
        return urlunparse(
            parsed._replace(scheme=parsed.scheme.lower(), netloc=_normalize_netloc(parsed))
        )

    def get_metadata(self, url: str, content: str | None = None) -> dict[str, str]:
        """Extract metadata from generic URL (HA Core Parity with Smart Fallback)."""
        parsed = urlparse(url)
        author = parsed.hostname.lower() if parsed.hostname else "imported"
        path_parts = [p for p in parsed.path.strip("/").split("/") if p]
        last_part = path_parts[-1] if path_parts else ""

        if last_part.lower().endswith((".yaml", ".yml")):
            name = Path(last_part).stem
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
            canonical_url = self.canonicalize_url(url)
            short_sha = hashlib.sha256(canonical_url.encode()).hexdigest()[:7]
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
        self._host_to_provider: dict[str, SourceProvider] = {}
        self._build_host_index()

    def _build_host_index(self) -> None:
        """Pre-compute hostname→provider O(1) index for known domains.

        Uses a direct constant-time mapping from canonical hostnames to
        provider types, including DOMAIN_GITHUB_RAW for raw.githubusercontent.com
        URLs which are the most common URL type in practice.
        """
        host_to_provider_type: dict[str, type[SourceProvider]] = {
            DOMAIN_GITHUB: GitHubProvider,
            DOMAIN_GITHUB_RAW: GitHubProvider,
            DOMAIN_GIST: GistProvider,
            DOMAIN_HA_FORUM: HAForumProvider,
            DOMAIN_GITLAB: GitLabProvider,
            DOMAIN_CODEBERG: CodebergProvider,
            DOMAIN_BITBUCKET: BitbucketProvider,
        }
        provider_by_type = {type(p): p for p in self._providers}
        for host, ptype in host_to_provider_type.items():
            if ptype in provider_by_type:
                self._host_to_provider[host] = provider_by_type[ptype]

    def __iter__(self) -> Iterator[SourceProvider]:
        """Iterate over registered providers."""
        return iter(self._providers)

    def get_provider(self, url: str) -> SourceProvider | None:
        """Get the appropriate provider for the given URL."""
        try:
            hostname = _normalize_hostname(urlparse(url).hostname)
        except ValueError:
            return None
        if hostname and (provider := self._host_to_provider.get(hostname)):
            return provider

        for provider in self._providers:
            try:
                if not isinstance(provider, GenericProvider) and provider.can_handle(url):
                    return provider
            except ValueError:
                continue

        generic = next((p for p in self._providers if isinstance(p, GenericProvider)), None)
        try:
            return generic if generic and generic.can_handle(url) else None
        except ValueError:
            return None

    def normalize_url(self, url: str) -> str:
        """Find appropriate provider and normalize URL."""
        with contextlib.suppress(ValueError):
            if provider := self.get_provider(url):
                return provider.normalize_url(url)
        return url

    def canonicalize_url(self, url: str) -> str:
        """Find appropriate provider and canonicalize URL."""
        try:
            if provider := self.get_provider(url):
                return provider.canonicalize_url(url)
            # Fallback for URLs without a matching provider (no valid scheme/netloc).
            # Apply the same hostname and scheme normalization as SourceProvider.canonicalize_url
            # so callers always get a consistently normalized result.
            parsed = urlparse(url.strip())
            netloc = _normalize_netloc(parsed)
            return urlunparse(
                parsed._replace(
                    scheme=parsed.scheme.lower(),
                    netloc=netloc,
                    path=parsed.path.rstrip("/"),
                    fragment="",
                )
            )
        except ValueError:
            return url.strip()

    def are_same_source(self, url1: str | None, url2: str | None) -> bool:
        """Check if two URLs represent the exact same source.

        Returns False if either URL is so malformed that it cannot be parsed
        (e.g. an unclosed IPv6 bracket such as 'https://[::1/path'), rather than
        propagating the ValueError to the caller.  A URL that cannot be parsed
        has no meaningful canonical identity and must not match any other URL.
        Exact identical non-None strings match via the equality fast path.
        """
        if url1 is None or url2 is None:
            return url1 == url2
        if url1 == url2:
            return True
        try:
            provider1 = self.get_provider(url1)
            provider2 = self.get_provider(url2)
            if provider1 is not None and provider1 == provider2:
                return provider1.is_same_source(url1, url2)
            return self.canonicalize_url(url1) == self.canonicalize_url(url2)
        except ValueError:
            return False


registry = ProviderRegistry()
