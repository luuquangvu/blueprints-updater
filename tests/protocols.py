"""Protocols for Blueprints Updater testing.

These protocols define the internal and external interface of the BlueprintUpdateCoordinator
for type-safe test access.
"""

from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from homeassistant.core import HomeAssistant

from custom_components.blueprints_updater.coordinator import BlueprintMetadata


@runtime_checkable
class BlueprintCoordinatorPublic(Protocol):
    """Stable public API for the coordinator."""

    hass: Any
    config_entry: Any
    data: dict[str, Any]
    setup_complete: bool
    last_update_success: bool

    async def async_setup(self) -> None:
        """Execute initial setup logic."""
        ...

    async def async_shutdown(self) -> None:
        """Gracefully terminate background tasks."""
        ...

    async def async_translate(self, key: str, **kwargs: Any) -> str:
        """Translate a localizable string."""
        ...

    async def async_fetch_blueprint(self, path: str, *, force: bool = False) -> None:
        """Force a network refresh for a specific blueprint."""
        ...

    async def async_install_blueprint(
        self,
        path: str,
        remote_content: str,
        reload_services: bool = True,
        backup: bool = True,
    ) -> None:
        """Install a new blueprint or update an existing one."""
        ...

    async def async_reload_services(self, domains: list[str]) -> None:
        """Reload services associated with defined domains."""
        ...

    async def async_restore_blueprint(self, path: str, version: int = 1) -> dict[str, Any]:
        """Restore blueprint from a local backup file."""
        ...

    def get_cached_git_diff(
        self, path: str, local_hash: str | None, remote_hash: str | None
    ) -> str | None:
        """Get cached git diff."""
        ...

    def set_cached_git_diff(
        self, path: str, local_hash: str | None, remote_hash: str | None, diff_text: str
    ) -> None:
        """Set cached git diff."""
        ...

    async def async_fetch_diff_content(self, path: str) -> str | None:
        """Fetch remote content for diff generation."""
        ...

    async def async_get_git_diff(self, path: str) -> str | None:
        """Get or generate git diff for a blueprint."""
        ...

    @staticmethod
    def scan_blueprints(
        hass: HomeAssistant,
        filter_mode: str,
        selected_blueprints: list[str],
    ) -> dict[str, BlueprintMetadata]:
        """Statically scan local blueprints directory."""
        ...

    def async_add_listener(self, cb: Callable[[], None]) -> Callable[[], None]:
        """Register a callback for data updates."""
        ...


@runtime_checkable
class BlueprintCoordinatorInternal(Protocol):
    """Internal methods and state used in detailed integration tests.

    WARNING: These members are implementation details and may change without notice.
    Tests coupling to these should be localized and well-justified.
    """

    _listeners: dict[Any, Any]
    _store: Any
    _persisted_etags: dict[str, str]
    _persisted_hashes: dict[str, str]
    _last_request_time: float
    _background_task: Any

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        """Fetch blueprint update data (internal handler)."""
        ...

    def async_set_updated_data(self, data: dict[str, Any]) -> None:
        """Set the data in the coordinator."""
        ...

    def async_update_listeners(self) -> None:
        """Update any listeners with new data."""
        ...

    async def _async_save_metadata(self) -> None:
        """Save coordinator metadata to persistent storage."""
        ...

    async def _async_fetch_content(
        self,
        session: Any,
        url: str,
        etag: str | None = None,
        force: bool = False,
    ) -> tuple[str | None, str | None]:
        """Perform raw network fetch with pacing and retry logic."""
        ...

    async def _async_update_blueprint_in_place(
        self,
        session: Any,
        path: str,
        info: dict[str, Any],
        results_to_notify: list[str],
        updated_domains: set[str],
        force: bool = False,
    ) -> None:
        """Update a blueprint file in place."""
        ...

    async def _process_blueprint_content(
        self,
        path: str,
        info: dict[str, Any],
        remote_content: str,
        new_etag: str | None,
        source_url: str,
        results_to_notify: list[str],
        updated_domains: set[str],
    ) -> None:
        """Process results of a network fetch for a blueprint."""
        ...

    async def _async_background_refresh(self, blueprints: dict[str, Any]) -> None:
        """Perform a background refresh of all blueprints."""
        ...

    def _is_safe_path(self, path: str) -> bool:
        """Check if path is within blueprints directory."""
        ...

    async def _is_safe_url(self, url: str) -> bool:
        """Check if the URL is safe."""
        ...

    def _validate_blueprint(self, blueprint_dict: dict[str, Any], source_url: str) -> str | None:
        """Validate blueprint structure and return error tag if invalid."""
        ...

    @staticmethod
    def _normalize_url(url: str) -> str:
        """Normalize URL for consistent identifier usage."""
        ...

    @staticmethod
    def _parse_forum_content(json_data: dict[str, Any]) -> str | None:
        """Parse blueprint content from forum JSON responses."""
        ...

    @staticmethod
    def _ensure_source_url(content: str, source_url: str) -> str:
        """Inject or normalize source_url in blueprint metadata."""
        ...


@runtime_checkable
class BlueprintCoordinatorProtocol(
    BlueprintCoordinatorPublic,
    BlueprintCoordinatorInternal,
    Protocol,
):
    """Combined protocol for backward compatibility in test fixtures."""
