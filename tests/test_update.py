from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.blueprints_updater.const import DOMAIN
from custom_components.blueprints_updater.update import async_setup_entry


@pytest.mark.asyncio
async def test_update_entities_lifecycle(hass):
    """Test that entities are added and removed correctly."""
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.async_on_unload = MagicMock()

    coordinator = MagicMock()
    # Initial data with one blueprint
    coordinator.data = {
        "/config/blueprints/test1.yaml": {
            "name": "Test 1",
            "rel_path": "test1.yaml",
            "source_url": "https://url1.com",
            "local_hash": "hash1",
        }
    }
    coordinator.async_add_listener = MagicMock()

    hass.data = {DOMAIN: {"test_entry": coordinator}}
    hass.async_create_task = MagicMock()

    async_add_entities = MagicMock()

    # 1. Setup entry - should add initial entity
    await async_setup_entry(hass, entry, async_add_entities)

    assert async_add_entities.called
    added_entities = async_add_entities.call_args[0][0]
    assert len(added_entities) == 1
    entity = added_entities[0]
    assert entity._path == "/config/blueprints/test1.yaml"

    # Get the listener callback
    update_callback = coordinator.async_add_listener.call_args[0][0]

    # 2. Add another blueprint
    coordinator.data["/config/blueprints/test2.yaml"] = {
        "name": "Test 2",
        "rel_path": "test2.yaml",
        "source_url": "https://url2.com",
        "local_hash": "hash2",
    }

    async_add_entities.reset_mock()
    update_callback()

    assert async_add_entities.called
    added_entities = async_add_entities.call_args[0][0]
    assert len(added_entities) == 1
    entity2 = added_entities[0]
    assert entity2._path == "/config/blueprints/test2.yaml"

    # 3. Remove the first blueprint
    del coordinator.data["/config/blueprints/test1.yaml"]

    # Mock entity.async_remove
    entity.async_remove = AsyncMock()

    update_callback()

    # Verify entity removal was initiated via hass.async_create_task
    assert hass.async_create_task.called
    assert entity.async_remove.called

    # Get the coroutine that was passed to async_create_task and await it
    # to avoid RuntimeWarning and verify it's the right one.
    remove_coro = hass.async_create_task.call_args[0][0]
    await remove_coro
    # Alternative check: just verify pop and call happened implicitly if we can
    # Given the implementation: hass.async_create_task(entity.async_remove())
    # We can check if async_remove was called (which returns the coro passed to async_create_task)
    assert entity.async_remove.called
