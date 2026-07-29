"""Integration tests for supported Home Assistant test environments."""

from pathlib import Path

from homeassistant.core import HomeAssistant


async def test_hass_uses_isolated_config_directory(
    hass: HomeAssistant,
    tmp_path: Path,
) -> None:
    """Keep legacy and current HA test runs isolated from shared package data."""
    assert Path(hass.config.config_dir) == tmp_path
