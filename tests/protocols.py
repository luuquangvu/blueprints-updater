"""Protocols for Blueprints Updater testing.

These protocols define the internal and external interface of the BlueprintUpdateCoordinator
for type-safe test access.
"""

from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from homeassistant.core import HomeAssistant

from custom_components.blueprints_updater.coordinator import BlueprintMetadata


@runtime_checkable
class BlueprintCoordinatorCore(Protocol):
    """Core coordinator attributes and status methods."""

    hass: Any
    config_entry: Any
    data: dict[str, Any]
    setup_complete: bool
    last_update_success: bool
    _listeners: dict[Any, Any]

    async def async_setup(self) -> None:
        """Execute initial setup logic."""
        ...

    async def async_refresh(self) -> None:
        """Trigger an manual data refresh."""
        ...

    def async_add_listener(self, cb: Callable[[], None]) -> Callable[[], None]:
        """Register a callback for data updates."""
        ...

    def async_set_updated_data(self, data: dict[str, Any]) -> None:
        """Set the data in the coordinator."""
        ...

    def async_update_listeners(self) -> None:
        """Update any listeners with new data."""
        ...

    async def async_translate(self, key: str, **kwargs: Any) -> str:
        """Translate a localizable string."""
        ...


@runtime_checkable
class BlueprintCoordinatorPersistence(Protocol):
    """Attributes and methods related to state persistence."""

    _store: Any
    _persisted_etags: dict[str, str]
    _persisted_hashes: dict[str, str]

    async def _async_save_metadata(self) -> None:
        """Save coordinator metadata to persistent storage."""
        ...


@runtime_checkable
class BlueprintCoordinatorFetch(Protocol):
    """Logic for network fetching and content processing."""

    async def _async_fetch_content(
        self,
        session: Any,
        url: str,
        etag: str | None = None,
        force: bool = False,
    ) -> tuple[str | None, str | None]:
        """Perform raw network fetch with pacing and retry logic."""
        ...

    async def async_fetch_blueprint(self, path: str, *, force: bool = False) -> None:
        """Force a network refresh for a specific blueprint."""
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


@runtime_checkable
class BlueprintCoordinatorTasks(Protocol):
    """Background task management and safety checks."""

    _last_request_time: float
    _background_task: Any

    def _start_background_refresh(self) -> None:
        """Initiate background refresh task."""
        ...

    async def _async_background_refresh(self, blueprints: dict[str, Any]) -> None:
        """Perform a background refresh of all blueprints."""
        ...

    async def async_shutdown(self) -> None:
        """Gracefully terminate background tasks."""
        ...

    def _is_safe_path(self, path: str) -> bool:
        """Check if path is within blueprints directory."""
        ...

    async def _is_safe_url(self, url: str) -> bool:
        """Check if the URL is safe."""
        ...


@runtime_checkable
class BlueprintCoordinatorProtocol(
    BlueprintCoordinatorCore,
    BlueprintCoordinatorPersistence,
    BlueprintCoordinatorFetch,
    BlueprintCoordinatorTasks,
    Protocol,
):
    """Combined protocol using inheritance for testing convenience.

    This protocol exposes private methods and attributes to the test suite
    in a type-safe manner, avoiding the need for # type: ignore or cast(Any, ...).
    """

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        """Async update method used by Home Assistant DataUpdateCoordinator."""
        ...

    def _validate_blueprint(self, blueprint_dict: dict[str, Any], source_url: str) -> str | None:
        """Validate blueprint structure and return error tag if invalid."""
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

    @staticmethod
    def scan_blueprints(
        hass: HomeAssistant,
        filter_mode: str,
        selected_blueprints: list[str],
    ) -> dict[str, BlueprintMetadata]:
        """Statically scan local blueprints directory."""
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
    def _parse_blueprint_data(path: str, content: str) -> Any:
        """Parse raw YAML content and extract blueprint metadata if valid."""
        ...

    @staticmethod
    def _ensure_source_url(content: str, source_url: str) -> str:
        """Inject or normalize source_url in blueprint metadata."""
        ...

    @staticmethod
    def _normalize_domain(domain: Any) -> str:
        """Normalize domain string for Home Assistant reload services."""
        ...

    @staticmethod
    def _should_include_blueprint(rel_path: str, filter_mode: str, selected_set: set[str]) -> bool:
        """Determine if a blueprint file should be processed."""
        ...

    @staticmethod
    def _get_blueprint_block(path: str, content: str) -> dict[str, Any] | None:
        """Extract the 'blueprint' metadata block from YAML content."""
        ...
