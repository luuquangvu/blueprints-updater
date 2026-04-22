"""Tests for coordinator metadata handling logic."""

from typing import Any, cast
from unittest.mock import mock_open, patch

import pytest

from custom_components.blueprints_updater.coordinator import GitDiffResult


def test_normalize_url(coordinator):
    """Test URL normalization."""
    assert (
        coordinator._normalize_url("https://github.com/user/repo/blob/main/blueprints/test.yaml")
        == "https://raw.githubusercontent.com/user/repo/main/blueprints/test.yaml"
    )

    assert (
        coordinator._normalize_url("https://gist.github.com/user/gist_id")
        == "https://gist.github.com/user/gist_id/raw"
    )

    assert (
        coordinator._normalize_url("https://gist.github.com/user/gist_id/raw")
        == "https://gist.github.com/user/gist_id/raw"
    )

    assert (
        coordinator._normalize_url("https://community.home-assistant.io/t/topic-slug/12345")
        == "https://community.home-assistant.io/t/12345.json"
    )

    assert (
        coordinator._normalize_url("https://example.com/blueprint.yaml")
        == "https://example.com/blueprint.yaml"
    )


def test_ensure_source_url(coordinator):
    """Test ensuring source_url is present."""
    source_url = "https://github.com/user/repo/blob/main/test.yaml"

    new_content = coordinator._ensure_source_url("blueprint:\n  name: Test", source_url)
    assert f"source_url: {source_url}" in new_content
    import yaml

    parsed = yaml.safe_load(new_content)
    assert parsed["blueprint"]["source_url"] == source_url
    assert parsed["blueprint"]["name"] == "Test"

    content_with_url = f"blueprint:\n  name: Test\n  source_url: {source_url}"
    result = coordinator._ensure_source_url(content_with_url, source_url)
    assert f"source_url: {source_url}" in result

    content_with_quotes = f"blueprint:\n  name: Test\n  source_url: '{source_url}'"
    result_quotes = coordinator._ensure_source_url(content_with_quotes, source_url)
    assert source_url in result_quotes

    different_url = "https://github.com/user/new-repo/blob/main/test.yaml"
    content_different = f"blueprint:\n  name: Test\n  source_url: {different_url}"
    result = coordinator._ensure_source_url(content_different, source_url)
    assert f"source_url: {source_url}" in result
    assert different_url not in result
    assert result.count("source_url") == 1

    content_outside = (
        "blueprint:\n  name: Test\n  domain: automation\n"
        "action:\n  - service: rest.post\n    data:\n"
        "      source_url: https://api.example.com"
    )
    result_outside = coordinator._ensure_source_url(content_outside, source_url)
    assert f"source_url: {source_url}" in result_outside
    parsed_outside = yaml.safe_load(result_outside)
    assert parsed_outside["blueprint"]["source_url"] == source_url
    assert parsed_outside["action"][0]["data"]["source_url"] == "https://api.example.com"

    content_nested_input = (
        "blueprint:\n  name: Test\n  domain: automation\n"
        "  input:\n    source_url:\n      name: Enter URL\n"
        "trigger:\n  - platform: webhook"
    )
    result_nested = coordinator._ensure_source_url(content_nested_input, source_url)
    assert f"source_url: {source_url}" in result_nested
    assert result_nested.count("source_url") == 2

    content_with_comment = "blueprint: # comment\n  name: Test"
    result_comment = coordinator._ensure_source_url(content_with_comment, source_url)
    assert f"source_url: {source_url}" in result_comment

    content_flow = "blueprint: { name: Test }"
    result_flow = coordinator._ensure_source_url(content_flow, source_url)
    assert f"source_url: {source_url}" in result_flow

    content_invalid = "\ufeffblueprint: [unclosed\r\n"
    result_invalid = coordinator._ensure_source_url(content_invalid, source_url)
    assert "\ufeff" not in result_invalid
    assert "\r" not in result_invalid

    content_multi = (
        "# Some info: blueprint:\n"
        "blueprint:\n"
        "  name: Test\n"
        "description: 'This is another blueprint: key in string'"
    )
    result_multi = coordinator._ensure_source_url(content_multi, source_url)
    assert f"source_url: {source_url}" in result_multi
    assert "source_url:" in result_multi.split("description:")[0]

    content_none = "not_a_blueprint: true"
    expected_none = coordinator._normalize_content(content_none)
    assert coordinator._ensure_source_url(content_none, source_url) == expected_none


def test_ensure_source_url_prioritizes_local(coordinator):
    """Test that local source_url overwrites a different one in remote content."""
    local_url = "https://github.com/local/link"
    remote_url = "https://github.com/remote/link"
    content = f"blueprint:\n  name: Test\n  source_url: {remote_url}\n  author: Me"

    result = coordinator._ensure_source_url(content, local_url)

    assert local_url in result
    assert remote_url not in result
    assert "name: Test" in result
    assert "author: Me" in result

    assert coordinator._ensure_source_url(result, local_url) == coordinator._normalize_content(
        result
    )


@pytest.mark.parametrize(
    "variant, input_content, expected_output",
    [
        ("standard", "blueprint:\n  name: Test", "blueprint:\n  name: Test"),
        ("BOM", "\ufeffblueprint:\n  name: Test", "blueprint:\n  name: Test"),
        ("CRLFs", "blueprint:\r\n  name: Test\r\n", "blueprint:\n  name: Test\n"),
        ("Classic Mac", "blueprint:\r  name: Test\r", "blueprint:\n  name: Test\n"),
        ("Spaced", "blueprint:  \n  name: Test ", "blueprint:  \n  name: Test "),
        ("Extra lines", "\n\nblueprint:\n  name: Test\n", "\n\nblueprint:\n  name: Test\n"),
    ],
)
def test_normalization_comprehensive(coordinator, variant, input_content, expected_output):
    """Test that normalization handle various encodings and formats consistently."""
    normalized = coordinator._normalize_content(input_content)
    assert normalized == expected_output, f"Failed for variant: {variant}"

    hash1 = coordinator._hash_content(input_content)
    hash2 = coordinator._hash_content(normalized, already_normalized=True)
    assert hash1 == hash2, f"Hash mismatch for variant: {variant}"


def test_normalization_idempotency(coordinator):
    """Test that normalization is idempotent: normalize(normalize(x)) == normalize(x)."""
    content = "blueprint:\n  name: Test   \r\n  source_url: https://url\n\n"
    first = coordinator._normalize_content(content)
    second = coordinator._normalize_content(first)
    assert first == second
    assert coordinator._hash_content(first) == coordinator._hash_content(second)


def test_ensure_source_url_stability(coordinator):
    """Test that injection is stable."""
    source_url = "https://url.com"
    content = "blueprint: # comment  \n  name: Test"

    injected = coordinator._ensure_source_url(content, source_url)
    assert "source_url: https://url.com" in injected
    re_injected = coordinator._ensure_source_url(injected, source_url)
    assert injected == re_injected


def test_validate_blueprint_valid(coordinator):
    """Test _validate_blueprint with valid data returns None."""
    data = {
        "blueprint": {
            "name": "Test",
            "domain": "automation",
            "input": {},
        },
        "trigger": [],
        "action": [],
    }
    coordinator.hass.data = {}
    result = coordinator._validate_blueprint(data, "https://example.com/bp.yaml")
    assert result is None


def test_validate_blueprint_incompatible_version(coordinator):
    """Test _validate_blueprint blocks when min_version is too high."""
    data = {
        "blueprint": {
            "name": "Test",
            "domain": "automation",
            "input": {},
            "homeassistant": {"min_version": "2099.1.0"},
        },
        "trigger": [],
        "action": [],
    }
    coordinator.hass.data = {}
    result = coordinator._validate_blueprint(data, "https://example.com/bp.yaml")
    assert result is not None
    assert "incompatible" in result
    assert "2099.1.0" in result


def test_validate_blueprint_schema_error(coordinator):
    """Test _validate_blueprint catches schema validation errors."""
    data = {"blueprint": {"name": "Test"}}
    coordinator.hass.data = {}
    result = coordinator._validate_blueprint(data, "https://example.com/bp.yaml")
    assert result is not None
    assert "validation_error" in result


def test_validate_blueprint_missing_key(coordinator):
    """Test _validate_blueprint with data missing the 'blueprint' key."""
    coordinator.hass.data = {}
    result = coordinator._validate_blueprint({"not_blueprint": {}}, "https://example.com/bp.yaml")
    assert result is not None
    assert "invalid_blueprint" in result


def test_ensure_source_url_structured_modification(coordinator):
    """Test that _ensure_source_url prefers structured YAML modification."""
    content = "blueprint:\n  name: Test\n"
    source_url = "https://example.com/bp.yaml"

    result = coordinator._ensure_source_url(content, source_url)
    assert "source_url: https://example.com/bp.yaml" in result

    from homeassistant.util import yaml as yaml_util

    parsed = yaml_util.parse_yaml(result)
    assert isinstance(parsed, dict)
    assert isinstance(parsed["blueprint"], dict)
    assert parsed["blueprint"]["source_url"] == source_url
    assert parsed["blueprint"]["name"] == "Test"


def test_hash_content_determinism(coordinator):
    """Test that hashing is deterministic regardless of the already_normalized flag."""
    content = "\ufeffblueprint:\r\n  name: Test\n"
    hash1 = coordinator._hash_content(content)

    normalized = coordinator._normalize_content(content)
    hash2 = coordinator._hash_content(normalized, already_normalized=True)

    assert hash1 == hash2
    assert "\ufeff" not in normalized
    assert "\r\n" not in normalized


@pytest.mark.asyncio
async def test_async_get_git_diff_cache_hit(coordinator):
    """Test async_get_git_diff returns cached value if hashes match."""
    path = "automation/test.yaml"
    coordinator.data = {
        path: {
            "local_hash": "h1",
            "remote_hash": "h2",
            "_cached_git_diff": {"local": "h1", "remote": "h2", "diff": "cached_diff"},
        }
    }
    expected = GitDiffResult(diff_text="cached diff", is_semantic_sync=False)
    with patch.object(coordinator, "get_cached_git_diff", return_value=expected):
        res = await coordinator.async_get_git_diff(path)
        assert res == expected


@pytest.mark.asyncio
async def test_async_get_git_diff_full_flow(coordinator):
    """Test async_get_git_diff fetches and generates diff on cache miss."""
    path = "automation/test.yaml"
    coordinator.data = {
        path: {
            "local_hash": "h1",
            "remote_hash": "h2",
            "source_url": "https://url.com",
            "updatable": True,
        }
    }

    local_content = "blueprint:\n  name: Old"
    remote_content = "blueprint:\n  name: New"

    with (
        patch.object(coordinator, "async_fetch_diff_content", return_value=remote_content),
        patch("builtins.open", mock_open(read_data=local_content)),
    ):
        res = await coordinator.async_get_git_diff(path)
        assert res is not None
        assert res.is_semantic_sync is False
        assert "+  name: New" in res.diff_text
        diff = res.diff_text
        assert coordinator.data[path]["_cached_git_diff"] == {
            "local": "h1",
            "remote": "h2",
            "diff": diff,
            "semantic_sync": False,
        }


@pytest.mark.asyncio
async def test_async_get_git_diff_shows_metadata_changes(coordinator):
    """Test that source_url differences ARE shown in Git diff."""
    path = "automation/test.yaml"
    source_url = "https://new.com"
    coordinator.data = {
        path: {
            "local_hash": "h1",
            "remote_hash": "h2",
            "source_url": source_url,
            "updatable": True,
        }
    }

    local_content = "blueprint:\n  source_url: https://old.com\n  name: Same"
    remote_content = "blueprint:\n  source_url: https://new.com\n  name: Same"

    with (
        patch.object(coordinator, "async_fetch_diff_content", return_value=remote_content),
        patch("builtins.open", mock_open(read_data=local_content)),
    ):
        res = await coordinator.async_get_git_diff(path)
        assert res is not None
        assert res.is_semantic_sync is False
        assert "-  source_url: https://old.com" in res.diff_text
        assert "+  source_url: https://new.com" in res.diff_text


@pytest.mark.asyncio
async def test_cached_git_diff_semantic_sync(coordinator):
    """Test git diff cache with semantic_sync flag."""
    path = "/config/test_semantic.yaml"
    local = "local_semantic"
    remote = "remote_semantic"
    diff_text = "semantic diff content"

    coordinator.data = {
        path: {
            "local_hash": local,
            "remote_hash": remote,
            "source_url": "https://url.com",
        }
    }

    coordinator.set_cached_git_diff(
        path,
        local,
        remote,
        diff_text,
        is_semantic_sync=True,
    )

    cached = cast(dict[str, Any], coordinator.data[path]["_cached_git_diff"])
    assert cached["semantic_sync"] is True
    assert coordinator.get_cached_git_diff(path, local, remote) == GitDiffResult(
        diff_text=diff_text, is_semantic_sync=True
    )
    res = await coordinator.async_get_git_diff(path)
    assert res is not None
    assert res.is_semantic_sync is True
    assert res.diff_text == diff_text


def test_get_cached_git_diff(coordinator):
    """Test get_cached_git_diff logic."""
    path = "automation/test.yaml"
    coordinator.data = {
        path: {"_cached_git_diff": {"local": "local", "remote": "remote", "diff": "diff"}}
    }
    assert coordinator.get_cached_git_diff(path, "local", "remote") == GitDiffResult(
        diff_text="diff", is_semantic_sync=False
    )
    assert coordinator.get_cached_git_diff(path, "wrong", "remote") is None
    assert coordinator.get_cached_git_diff("missing", "local", "remote") is None


def test_set_cached_git_diff(coordinator):
    """Test set_cached_git_diff logic."""
    path = "automation/test.yaml"
    coordinator.data = {path: {}}
    coordinator.set_cached_git_diff(path, "l1", "r1", "d1")
    assert coordinator.data[path]["_cached_git_diff"] == {
        "local": "l1",
        "remote": "r1",
        "diff": "d1",
        "semantic_sync": False,
    }
