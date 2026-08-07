"""Direct unit tests for BlueprintFileStore atomic transactions and backup rotation."""

import asyncio
import os
from pathlib import Path

import pytest

from custom_components.blueprints_updater.exceptions import FileRevisionMismatchError
from custom_components.blueprints_updater.file_store import (
    BlueprintFileStore,
    FileRevisionPrecondition,
)


@pytest.mark.asyncio
async def test_file_store_atomic_install_and_rotate_backups(tmp_path: Path) -> None:
    """Test installing content atomically and rotating backups up to max_backups."""
    target_file = tmp_path / "test_blueprint.yaml"
    file_path = str(target_file)

    initial_content = "blueprint:\n  name: Version 1\n"
    res1 = BlueprintFileStore.install(
        file_path=file_path,
        content=initial_content,
        max_backups=2,
        create_backup=False,
        precondition=FileRevisionPrecondition.missing(),
    )
    assert target_file.read_text(encoding="utf-8") == initial_content
    assert res1.backups_count == 0

    precondition1 = BlueprintFileStore.capture_precondition(file_path)
    assert precondition1.must_exist is True
    assert precondition1.content_hash == res1.content_hash

    v2_content = "blueprint:\n  name: Version 2\n"
    res2 = BlueprintFileStore.install(
        file_path=file_path,
        content=v2_content,
        max_backups=2,
        create_backup=True,
        precondition=precondition1,
    )
    assert target_file.read_text(encoding="utf-8") == v2_content
    assert res2.backups_count == 1
    assert BlueprintFileStore.read_backup(file_path, 1) == initial_content

    precondition2 = BlueprintFileStore.capture_precondition(file_path)

    v3_content = "blueprint:\n  name: Version 3\n"
    res3 = BlueprintFileStore.install(
        file_path=file_path,
        content=v3_content,
        max_backups=2,
        create_backup=True,
        precondition=precondition2,
    )
    assert target_file.read_text(encoding="utf-8") == v3_content
    assert res3.backups_count == 2
    assert BlueprintFileStore.read_backup(file_path, 1) == v2_content
    assert BlueprintFileStore.read_backup(file_path, 2) == initial_content


@pytest.mark.asyncio
async def test_file_store_precondition_mismatch_raises(tmp_path: Path) -> None:
    """Test that a stale content hash precondition raises FileRevisionMismatchError."""
    target_file = tmp_path / "mismatch_blueprint.yaml"
    file_path = str(target_file)

    target_file.write_text("blueprint:\n  name: Original\n", encoding="utf-8")

    stale_precondition = FileRevisionPrecondition.existing("0" * 64)

    with pytest.raises(FileRevisionMismatchError, match="content changed"):
        BlueprintFileStore.install(
            file_path=file_path,
            content="blueprint:\n  name: New\n",
            max_backups=3,
            create_backup=True,
            precondition=stale_precondition,
        )


@pytest.mark.asyncio
async def test_file_store_restore_prevalidated_content(tmp_path: Path) -> None:
    """Test restoring prevalidated content while preserving current file as backup."""
    target_file = tmp_path / "restore_target.yaml"
    file_path = str(target_file)

    target_file.write_text("blueprint:\n  name: Current Corrupted\n", encoding="utf-8")
    precondition = BlueprintFileStore.capture_precondition(file_path)

    good_content = "blueprint:\n  name: Known Good\n"
    res = BlueprintFileStore.restore(
        file_path=file_path,
        validated_content=good_content,
        max_backups=3,
        precondition=precondition,
    )

    assert target_file.read_text(encoding="utf-8") == good_content
    assert res.backups_count == 1
    assert "Corrupted" in BlueprintFileStore.read_backup(file_path, 1)


@pytest.mark.asyncio
async def test_file_store_path_transaction_lock(tmp_path: Path) -> None:
    """Test serializing path mutations through path locks with concurrent tasks."""
    target_file = tmp_path / "lock_test.yaml"
    file_path = str(target_file)

    task1_entered = asyncio.Event()
    task1_release = asyncio.Event()
    task2_entered = asyncio.Event()

    async def _task1() -> None:
        """Acquire transaction lock and signal entry."""
        async with BlueprintFileStore.transaction(file_path):
            target_file.write_text("blueprint:\n  name: Task 1\n", encoding="utf-8")
            task1_entered.set()
            await task1_release.wait()

    async def _task2() -> None:
        """Acquire transaction lock after task1 releases."""
        async with BlueprintFileStore.transaction(file_path):
            task2_entered.set()
            target_file.write_text("blueprint:\n  name: Task 2\n", encoding="utf-8")

    t1 = asyncio.create_task(_task1())
    await task1_entered.wait()

    t2 = asyncio.create_task(_task2())
    await asyncio.sleep(0.01)

    assert not task2_entered.is_set()
    assert target_file.read_text(encoding="utf-8") == "blueprint:\n  name: Task 1\n"
    assert os.path.exists(file_path)

    task1_release.set()
    await asyncio.gather(t1, t2)

    assert task2_entered.is_set()
    assert target_file.read_text(encoding="utf-8") == "blueprint:\n  name: Task 2\n"
