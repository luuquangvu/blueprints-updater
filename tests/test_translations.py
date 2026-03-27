"""Tests for translation synchronization and quality."""

from __future__ import annotations

import json
import os
import re
from typing import Any

import pytest

TRANSLATIONS_DIR = "custom_components/blueprints_updater/translations"
STRINGS_FILE = "custom_components/blueprints_updater/strings.json"


def get_kv_in_order(d: dict[str, Any], prefix: str = "") -> list[tuple[str, Any]]:
    """Get keys and values as a list of tuples to preserve and check order."""
    kv = []
    for k, v in d.items():
        full_key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            kv.extend(get_kv_in_order(v, full_key))
        else:
            kv.append((full_key, v))
    return kv


def get_translation_files() -> list[str]:
    """Get all translation files including strings.json."""
    files = [
        os.path.join(TRANSLATIONS_DIR, f)
        for f in os.listdir(TRANSLATIONS_DIR)
        if f.endswith(".json")
    ]
    return files


@pytest.fixture
def strings_data() -> dict[str, Any]:
    """Load the master strings.json data."""
    with open(STRINGS_FILE, encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def strings_kv(strings_data: dict[str, Any]) -> list[tuple[str, Any]]:
    """Get flat KV list from strings.json."""
    return get_kv_in_order(strings_data)


@pytest.mark.parametrize("lang_file", get_translation_files())
def test_translation_sync(lang_file: str, strings_kv: list[tuple[str, Any]]) -> None:
    """Verify that each translation file has the exact same keys as strings.json."""
    with open(lang_file, encoding="utf-8") as f:
        lang_data = json.load(f)

    lang_kv = get_kv_in_order(lang_data)

    source_keys = [k for k, v in strings_kv]
    target_keys = [k for k, v in lang_kv]

    assert set(source_keys) == set(target_keys), f"Key mismatch in {lang_file}"


@pytest.mark.parametrize("lang_file", get_translation_files())
def test_translation_key_order(lang_file: str, strings_kv: list[tuple[str, Any]]) -> None:
    """Verify that translation files follow the same key order as strings.json."""
    with open(lang_file, encoding="utf-8") as f:
        lang_data = json.load(f)

    lang_kv = get_kv_in_order(lang_data)

    source_keys = [k for k, v in strings_kv]
    target_keys = [k for k, v in lang_kv]

    assert source_keys == target_keys, f"Key order mismatch in {lang_file}"


@pytest.mark.parametrize("lang_file", get_translation_files())
def test_translation_placeholders(lang_file: str, strings_kv: list[tuple[str, Any]]) -> None:
    """Verify that all placeholders in translations match the source."""
    with open(lang_file, encoding="utf-8") as f:
        lang_data = json.load(f)

    lang_kv_dict = dict(get_kv_in_order(lang_data))
    source_kv_dict = dict(strings_kv)

    for key, source_val in source_kv_dict.items():
        if key not in lang_kv_dict:
            continue

        target_val = lang_kv_dict[key]
        if not isinstance(source_val, str) or not isinstance(target_val, str):
            continue

        source_placeholders = set(re.findall(r"\{(\w+)\}", source_val))
        target_placeholders = set(re.findall(r"\{(\w+)\}", target_val))

        assert source_placeholders == target_placeholders, (
            f"Placeholder mismatch for {key} in {lang_file}: "
            f"expected {source_placeholders}, got {target_placeholders}"
        )


@pytest.mark.parametrize("lang_file", get_translation_files())
def test_no_english_pluralization_leaks(lang_file: str) -> None:
    """Verify that localized files (except English) don't contain '(s)' leaks."""

    if "en.json" in lang_file or lang_file == STRINGS_FILE:
        return

    with open(lang_file, encoding="utf-8") as f:
        content = f.read()

    assert "(s)" not in content, f"Found English-style pluralization leak '(s)' in {lang_file}"
