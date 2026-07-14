"""Serialized atomic blueprint filesystem transactions."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import shutil
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import ClassVar
from weakref import WeakValueDictionary

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class FileTransactionResult:
    """Verified result of an install or restore transaction."""

    content_hash: str
    backups_count: int


class BlueprintFileStore:
    """Own per-path locking and atomic blueprint file mutations."""

    _path_locks: ClassVar[WeakValueDictionary[str, asyncio.Lock]] = WeakValueDictionary()

    @classmethod
    @asynccontextmanager
    async def transaction(cls, path: str) -> AsyncIterator[None]:
        """Serialize every mutation of one canonical blueprint path."""
        canonical_path = os.path.realpath(path)
        lock = cls._path_locks.setdefault(canonical_path, asyncio.Lock())
        async with lock:
            yield

    @staticmethod
    def backup_path(file_path: str, version: int | str) -> str:
        """Return the numbered backup path for a blueprint."""
        return f"{file_path}.bak.{version}"

    @staticmethod
    def count_backups(file_path: str, max_backups: int) -> int:
        """Count existing numbered backups up to the configured limit."""
        return sum(
            os.path.isfile(BlueprintFileStore.backup_path(file_path, version))
            for version in range(1, max_backups + 1)
        )

    @staticmethod
    def read_backup(file_path: str, version: int) -> str:
        """Read one backup as strict UTF-8 before any filesystem mutation."""
        with open(BlueprintFileStore.backup_path(file_path, version), encoding="utf-8") as file:
            return file.read()

    @staticmethod
    def _new_temp_path(file_path: str) -> tuple[int, str]:
        """Create a unique temporary file in the target directory."""
        directory = os.path.dirname(file_path)
        os.makedirs(directory, exist_ok=True)
        return tempfile.mkstemp(
            prefix=f".{os.path.basename(file_path)}.",
            suffix=".tmp",
            dir=directory,
        )

    @staticmethod
    def _write_temp(file_path: str, content: str) -> tuple[str, str]:
        """Write, flush, and hash UTF-8 content in a unique temporary file."""
        descriptor, temp_path = BlueprintFileStore._new_temp_path(file_path)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        stream = None
        try:
            stream = os.fdopen(descriptor, "w", encoding="utf-8")
            with stream as file:
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
        except Exception:
            if stream is None:
                with contextlib.suppress(OSError):
                    os.close(descriptor)
            else:
                with contextlib.suppress(OSError):
                    stream.close()
            with contextlib.suppress(OSError):
                os.remove(temp_path)
            raise
        return temp_path, digest

    @staticmethod
    def _hash_file(file_path: str) -> str:
        """Hash a file without buffering it in memory."""
        digest = hashlib.sha256()
        with open(file_path, "rb") as file:
            while chunk := file.read(64 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def rotate_backups(file_path: str, max_backups: int) -> None:
        """Create and verify the newest backup, failing closed on integrity errors."""
        if not os.path.isfile(file_path):
            return

        directory = os.path.dirname(file_path)
        prefix = f"{os.path.basename(file_path)}.bak."
        stale_paths: list[str] = []
        with os.scandir(directory) as entries:
            for entry in entries:
                if not entry.name.startswith(prefix):
                    continue
                suffix = entry.name[len(prefix) :]
                if not suffix.isdigit() or int(suffix) <= max_backups:
                    continue
                try:
                    if entry.is_file():
                        stale_paths.append(entry.path)
                except OSError as err:
                    _LOGGER.warning("Failed to inspect stale backup %s: %s", entry.path, err)
        for stale_path in stale_paths:
            try:
                os.remove(stale_path)
            except OSError as err:
                _LOGGER.warning("Failed to remove stale backup %s: %s", stale_path, err)

        if max_backups <= 0:
            return

        descriptor, backup_temp = BlueprintFileStore._new_temp_path(file_path)
        os.close(descriptor)
        try:
            shutil.copy2(file_path, backup_temp)
            with open(backup_temp, "rb") as file:
                os.fsync(file.fileno())
            if BlueprintFileStore._hash_file(file_path) != BlueprintFileStore._hash_file(
                backup_temp
            ):
                raise OSError("Backup verification failed")

            oldest = BlueprintFileStore.backup_path(file_path, max_backups)
            with contextlib.suppress(FileNotFoundError):
                os.remove(oldest)
            for version in range(max_backups - 1, 0, -1):
                source = BlueprintFileStore.backup_path(file_path, version)
                destination = BlueprintFileStore.backup_path(file_path, version + 1)
                try:
                    os.replace(source, destination)
                except FileNotFoundError:
                    continue
            os.replace(backup_temp, BlueprintFileStore.backup_path(file_path, 1))
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.remove(backup_temp)

    @staticmethod
    def install(
        file_path: str,
        content: str,
        max_backups: int,
        create_backup: bool,
    ) -> FileTransactionResult:
        """Atomically install verified content after any requested backup succeeds."""
        temp_path, expected_hash = BlueprintFileStore._write_temp(file_path, content)
        try:
            installed_hash = BlueprintFileStore._hash_file(temp_path)
            if installed_hash != expected_hash:
                raise OSError("Installed blueprint verification failed")
            if create_backup:
                BlueprintFileStore.rotate_backups(file_path, max_backups)
            os.replace(temp_path, file_path)
            return FileTransactionResult(
                content_hash=installed_hash,
                backups_count=BlueprintFileStore.count_backups(file_path, max_backups),
            )
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.remove(temp_path)

    @staticmethod
    def restore(
        file_path: str,
        validated_content: str,
        max_backups: int,
    ) -> FileTransactionResult:
        """Restore prevalidated content while preserving the current file as a backup."""
        return BlueprintFileStore.install(
            file_path,
            validated_content,
            max_backups,
            create_backup=True,
        )
