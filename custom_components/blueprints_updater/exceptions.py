"""Custom exceptions for Blueprints Updater."""

from homeassistant.exceptions import HomeAssistantError

__all__ = [
    "BlueprintFetchPolicyError",
    "BlueprintRefreshObsoleteError",
    "BlueprintRestoreValidationError",
    "FileRevisionMismatchError",
]


class BlueprintFetchPolicyError(HomeAssistantError):
    """A deterministic remote-content policy failure that must not be retried."""


class BlueprintRefreshObsoleteError(HomeAssistantError):
    """Raised when background refresh work no longer owns its scan result."""


class BlueprintRestoreValidationError(HomeAssistantError):
    """Expected restore validation failure with a localized result contract."""

    def __init__(self, translation_key: str, **translation_kwargs: str) -> None:
        """Initialize a restore validation failure."""
        super().__init__(translation_key)
        self.result_translation_key = translation_key
        self.translation_kwargs = translation_kwargs


class FileRevisionMismatchError(OSError):
    """The target file no longer matches the caller's expected revision."""
