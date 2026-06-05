"""Tests for translation consistency and key ordering."""

from pathlib import Path

import orjson
import pytest

BASE_DIR = Path(__file__).parent.parent
STRINGS_JSON = BASE_DIR / "custom_components" / "blueprints_updater" / "strings.json"
TRANSLATIONS_DIR = BASE_DIR / "custom_components" / "blueprints_updater" / "translations"


def get_translation_files():
    """Return a list of all translation files."""
    return list(TRANSLATIONS_DIR.glob("*.json"))


def check_dict_order(data, ref, path=""):
    """Recursively check if dictionary keys match the reference order.

    This function performs the following checks:
    1. Verifies that all keys in the reference are present (no missing keys).
    2. Verifies that no extra keys are present in the data.
    3. Verifies that the order of keys exactly matches the reference.
    4. Recursively validates nested dictionaries.
    """
    data_keys = list(data.keys())
    ref_keys = list(ref.keys())

    missing = set(ref_keys) - set(data_keys)
    extra = set(data_keys) - set(ref_keys)

    assert not missing, f"Missing keys at '{path}': {missing}"
    assert not extra, f"Extra keys at '{path}': {extra}"

    assert data_keys == ref_keys, (
        f"Key order mismatch at '{path}'. Expected {ref_keys}, got {data_keys}"
    )

    for key in ref_keys:
        ref_is_dict = isinstance(ref[key], dict)
        data_is_dict = isinstance(data[key], dict)
        assert data_is_dict == ref_is_dict, (
            f"Type mismatch at '{path}.{key}'" if path else f"Type mismatch at '{key}'"
        )
        if ref_is_dict:
            new_path = f"{path}.{key}" if path else key
            check_dict_order(data[key], ref[key], new_path)


@pytest.mark.parametrize("translation_file", get_translation_files(), ids=lambda f: f.name)
def test_translation_key_order(translation_file):
    """Ensure translation file matches strings.json key order exactly.

    This test enforces strict structural and ordering parity. While some
    integrations allow omitting root sections in localizations, Blueprints
    Updater requires full alignment to ensure deterministic synchronization
    and clean diffs.
    """
    with open(STRINGS_JSON, encoding="utf-8") as f:
        reference = orjson.loads(f.read())

    with open(translation_file, encoding="utf-8") as f:
        translation = orjson.loads(f.read())

    check_dict_order(translation, reference)
