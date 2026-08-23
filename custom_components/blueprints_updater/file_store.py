"""Serialized atomic blueprint filesystem transactions."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import shutil
import stat
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import ClassVar
from weakref import WeakValueDictionary

from .exceptions import FileRevisionMismatchError

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class FileTransactionResult:
    """Verified result of an install or restore transaction."""

    content_hash: str
    backups_count: int


@dataclass(frozen=True)
class FileRevisionPrecondition:
    """Expected target state for one compare-and-swap filesystem mutation."""

    must_exist: bool
    content_hash: str | None = None

    def __post_init__(self) -> None:
        """Reject contradictory preconditions at construction time."""
        if self.must_exist and self.content_hash is None:
            raise ValueError("An existing-file precondition requires a content hash")
        if not self.must_exist and self.content_hash is not None:
            raise ValueError("A missing-file precondition cannot include a content hash")

    @classmethod
    def existing(cls, content_hash: str) -> FileRevisionPrecondition:
        """Require an existing regular file with the supplied content hash."""
        return cls(must_exist=True, content_hash=content_hash)

    @classmethod
    def missing(cls) -> FileRevisionPrecondition:
        """Require the target path to remain absent until commit."""
        return cls(must_exist=False)


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
    def _find_backup_files(file_path: str, min_version: int = 1) -> list[tuple[int, str]]:
        """Find all valid numbered backup files for a blueprint, sorted by version number.

        Args:
            file_path: Path to the blueprint file.
            min_version: Minimum version number to include (default 1).

        Returns:
            List of (version_number, backup_file_path) tuples.

        """
        directory = os.path.dirname(file_path)
        prefix = f"{os.path.basename(file_path)}.bak."
        if not os.path.isdir(directory):
            return []

        backups: list[tuple[int, str]] = []
        with os.scandir(directory) as entries:
            for entry in entries:
                if not entry.name.startswith(prefix):
                    continue
                suffix = entry.name[len(prefix) :]
                if not suffix.isdigit():
                    continue
                version = int(suffix)
                if version < min_version:
                    continue
                try:
                    if entry.is_file():
                        backups.append((version, entry.path))
                except OSError as err:
                    _LOGGER.warning("Failed to inspect stale backup %s: %s", entry.path, err)

        return sorted(backups, key=lambda x: x[0])

    @staticmethod
    def remove_blueprint_and_backups(file_path: str) -> None:
        """Remove a blueprint file and all its associated backups from disk."""
        if os.path.isfile(file_path):
            try:
                os.remove(file_path)
            except OSError as err:
                _LOGGER.warning("Failed to remove blueprint file '%s': %s", file_path, err)

        try:
            backup_files = BlueprintFileStore._find_backup_files(file_path)
        except OSError as err:
            _LOGGER.warning("Failed to scan directory for backups of '%s': %s", file_path, err)
            return

        for _, backup_path in backup_files:
            try:
                os.remove(backup_path)
            except OSError as err:
                _LOGGER.warning("Failed to remove blueprint backup '%s': %s", backup_path, err)

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
        no_follow = getattr(os, "O_NOFOLLOW", 0)

        def _open_no_follow(path: str, flags: int) -> int:
            """Open a file without following a final symlink when supported."""
            return os.open(path, flags | no_follow)

        with open(file_path, "rb", opener=_open_no_follow) as file:
            while chunk := file.read(64 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _validate_canonical_file_path(file_path: str) -> None:
        """Reject a symlink at the final path component."""
        if os.path.islink(file_path):
            raise FileRevisionMismatchError(
                "Blueprint path became a symlink after the operation was prepared"
            )

    @staticmethod
    def capture_precondition(file_path: str) -> FileRevisionPrecondition:
        """Capture the current regular-file revision for a later mutation."""
        BlueprintFileStore._validate_canonical_file_path(file_path)
        try:
            stat_result = os.stat(file_path, follow_symlinks=False)
        except FileNotFoundError:
            return FileRevisionPrecondition.missing()
        if not stat.S_ISREG(stat_result.st_mode):
            raise FileRevisionMismatchError("Blueprint target is not a regular file")
        return FileRevisionPrecondition.existing(BlueprintFileStore._hash_file(file_path))

    @staticmethod
    def _verify_current_revision(
        file_path: str,
        precondition: FileRevisionPrecondition | None,
        expected_identity: tuple[int, int, int, int] | None = None,
    ) -> tuple[int, int, int, int] | None:
        """Verify the target revision and return its stable filesystem identity."""
        BlueprintFileStore._validate_canonical_file_path(file_path)
        try:
            stat_result = os.stat(file_path, follow_symlinks=False)
        except FileNotFoundError:
            stat_result = None

        if precondition is not None and not precondition.must_exist:
            if stat_result is not None:
                raise FileRevisionMismatchError(
                    "Blueprint was created after the operation was prepared"
                )
            return None
        if precondition is not None and precondition.must_exist and stat_result is None:
            raise FileRevisionMismatchError(
                "Blueprint was deleted after the operation was prepared"
            )
        if stat_result is None:
            return None
        if not stat.S_ISREG(stat_result.st_mode):
            raise FileRevisionMismatchError("Blueprint target is not a regular file")
        identity = (
            stat_result.st_dev,
            stat_result.st_ino,
            stat_result.st_size,
            stat_result.st_mtime_ns,
        )
        if expected_identity is not None and identity != expected_identity:
            raise FileRevisionMismatchError("Blueprint changed after the operation was prepared")
        if (
            expected_identity is None
            and precondition is not None
            and precondition.content_hash is not None
            and BlueprintFileStore._hash_file(file_path) != precondition.content_hash
        ):
            raise FileRevisionMismatchError(
                "Blueprint content changed after the operation was prepared"
            )
        return identity

    @staticmethod
    def rotate_backups(file_path: str, max_backups: int) -> None:
        """Create and verify the newest backup, failing closed on integrity errors."""
        if not os.path.isfile(file_path):
            return

        for _, stale_path in BlueprintFileStore._find_backup_files(
            file_path, min_version=max_backups + 1
        ):
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
        precondition: FileRevisionPrecondition | None = None,
    ) -> FileTransactionResult:
        """Atomically install content if the target still has the expected revision."""
        BlueprintFileStore._validate_canonical_file_path(file_path)
        temp_path, expected_hash = BlueprintFileStore._write_temp(file_path, content)
        try:
            installed_hash = BlueprintFileStore._hash_file(temp_path)
            if installed_hash != expected_hash:
                raise OSError("Installed blueprint verification failed")
            identity = BlueprintFileStore._verify_current_revision(
                file_path,
                precondition,
            )
            if create_backup and (precondition is None or precondition.must_exist):
                BlueprintFileStore.rotate_backups(file_path, max_backups)
            BlueprintFileStore._verify_current_revision(
                file_path,
                precondition,
                expected_identity=identity,
            )
            if precondition is not None and not precondition.must_exist:
                try:
                    os.link(temp_path, file_path)
                except FileExistsError as err:
                    raise FileRevisionMismatchError(
                        "Blueprint was created after the operation was prepared"
                    ) from err
                os.remove(temp_path)
            else:
                # Without a precondition, callers accept last-writer-wins behavior:
                # a target created after the initial check may be replaced here.
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
        precondition: FileRevisionPrecondition | None = None,
    ) -> FileTransactionResult:
        """Restore prevalidated content while preserving the current file as a backup."""
        return BlueprintFileStore.install(
            file_path,
            validated_content,
            max_backups,
            create_backup=True,
            precondition=precondition,
        )
