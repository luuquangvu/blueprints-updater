"""Tests for translation consistency and key ordering."""

from pathlib import Path

import orjson
import pytest

from custom_components.blueprints_updater.const import DOMAIN

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


def test_no_translation_key_collisions():
    """Ensure no duplicate translation keys across categories after indexing.

    The ``async_translate`` method uses a flat index (category prefix
    stripped, ``.message`` suffix removed) for O(1) lookups.  If two
    categories defined the same key (e.g. ``exceptions.foo`` and
    ``common.foo``) the first-loaded value would silently win.

    This test simulates ``_build_translation_index`` on ``strings.json``
    and asserts zero collisions.  It runs in CI before every merge, so any
    future collision is caught at dev time rather than in production.
    """
    prefix = f"component.{DOMAIN}."

    def extract_index_keys(data, category):
        """Mimic _build_translation_index: collect leaf keys, strip prefix."""
        keys: dict[str, str] = {}

        def walk(d, path_parts):
            if not isinstance(d, dict):
                return
            for k, v in d.items():
                p = [*path_parts, k]
                if isinstance(v, str):
                    full_key = f"{prefix}{'.'.join(p)}"
                    if not full_key.startswith(prefix):
                        continue
                    suffix = full_key[len(prefix) :]
                    parts = suffix.split(".", 1)
                    if len(parts) != 2:
                        continue
                    cat = parts[0]
                    key = parts[1]
                    if key.endswith(".message"):
                        key = key[:-8]
                    if key not in keys:
                        keys[key] = f"{cat}.{parts[1]}"
                    else:
                        existing = keys[key]
                        raise AssertionError(
                            f"Translation key collision: {key!r} appears in "
                            f"both {existing!r} and {cat}.{parts[1]!r}. "
                            f"Rename one to avoid ambiguity in the flat index."
                        )
                else:
                    walk(v, p)

        walk(data.get(category, {}), [category])
        return keys

    # Load strings.json — the canonical reference
    with open(STRINGS_JSON, encoding="utf-8") as f:
        data = orjson.loads(f.read())

    # Verify: no cross-category collisions — collect all first for a clear report
    collisions: list[str] = []
    seen: dict[str, str] = {}
    for category in sorted(data):
        if not isinstance(data[category], dict):
            continue
        for key, source in extract_index_keys(data, category).items():
            if key in seen:
                collisions.append(f"  {key!r} in both {seen[key]!r} and {source!r}")
            else:
                seen[key] = source

    if collisions:
        raise AssertionError(
            f"Found {len(collisions)} cross-category translation key "
            f"collision(s):\n" + "\n".join(collisions) + "\n"
            "Rename the 'exceptions' variants to avoid ambiguity."
        )

    # This test is a guardrail — silence on success is enough
