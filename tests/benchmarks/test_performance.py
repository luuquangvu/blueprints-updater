"""Performance benchmarks for Blueprints Updater.

This module benchmarks key hot paths in the codebase to identify
bottlenecks, algorithmic inefficiencies, and optimization opportunities.

Run with:
    uv run pytest tests/benchmarks/test_performance.py -v --benchmark-only
    or
    uv run python -m pytest tests/benchmarks/test_performance.py -v -s
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import textwrap
import time
from collections import OrderedDict
from typing import Any, ClassVar, cast
from urllib.parse import urlparse

import pytest
from homeassistant.util import yaml as yaml_util

from custom_components.blueprints_updater.const import (
    FILTER_MODE_ALL,
    FILTER_MODE_BLACKLIST,
    FILTER_MODE_WHITELIST,
    BlueprintRiskType,
)
from custom_components.blueprints_updater.coordinator import (
    BlueprintUpdateCoordinator,
    StructuredRisk,
    _count_backups_sync_helper,
)
from custom_components.blueprints_updater.providers import (
    GitHubProvider,
    HAForumProvider,
    ProviderRegistry,
    _normalize_hostname,
    _replace_path_segment,
    registry,
)
from custom_components.blueprints_updater.utils import (
    get_config_value,
    get_validated_filter_mode,
    get_validated_selected_blueprints,
    normalize_domain,
    normalize_url,
    redact_url,
    sanitize_error_detail,
    should_include_blueprint,
)


@pytest.fixture
def sample_blueprint_content() -> str:
    """Return a realistic sample blueprint YAML content."""
    return textwrap.dedent("""\
        blueprint:
          name: Motion-activated Light
          description: Turn on a light when motion is detected
          domain: automation
          source_url: https://github.com/home-assistant/core/blob/dev/homeassistant/components/automation/blueprints/motion_light.yaml
          input:
            motion_entity:
              name: Motion Sensor
              selector:
                entity:
                  domain: binary_sensor
                  device_class: motion
            light_target:
              name: Light
              selector:
                target:
                  entity:
                    domain: light
            no_motion_wait:
              name: Wait time
              description: Time to leave the light on after last motion is detected.
              default: 120
              selector:
                number:
                  min: 0
                  max: 3600
                  unit_of_measurement: seconds
        trigger:
          platform: state
          entity_id: !input motion_entity
          from: "off"
          to: "on"
        action:
          - service: light.turn_on
            target: !input light_target
          - wait_for_trigger:
              platform: state
              entity_id: !input motion_entity
              from: "on"
              to: "off"
          - delay: !input no_motion_wait
          - service: light.turn_off
            target: !input light_target
        mode: restart
    """)


@pytest.fixture
def sample_blueprint_content_nested() -> str:
    """Return blueprint with nested input sections (HA 2024.6+)."""
    return textwrap.dedent("""\
        blueprint:
          name: Complex Automation
          domain: automation
          source_url: https://github.com/example/nested.yaml
          input:
            section_1:
              name: First Section
              input:
                sensor_a:
                  name: Sensor A
                  selector:
                    entity:
                      domain: binary_sensor
                sensor_b:
                  name: Sensor B
                  default: off
                  selector:
                    boolean:
            section_2:
              name: Second Section
              input:
                light_entity:
                  name: Target Light
                  selector:
                    entity:
                      domain: light
            brightness:
              name: Brightness Level
              default: 100
              selector:
                number:
                  min: 1
                  max: 255
        trigger:
          platform: state
        action:
          - service: light.turn_on
        mode: single
    """)


@pytest.fixture
def sample_blueprint_content_large() -> str:
    """Return a large blueprint with many inputs for stress testing."""
    inputs_block = [
        f"    input_{i}:\n      name: Input {i}\n"
        f"      selector:\n        entity:\n          domain: binary_sensor"
        for i in range(50)
    ]
    return (
        "blueprint:\n"
        "  name: Large Blueprint\n"
        "  domain: automation\n"
        "  source_url: https://github.com/example/large.yaml\n"
        "  input:\n" + "\n".join(inputs_block) + "\n"
        "trigger:\n"
        "  platform: state\n"
        "action:\n"
        "  - service: light.turn_on\n"
        "mode: single\n"
    )


@pytest.fixture
def blueprint_files_dir(tmp_path):
    """Create a directory with multiple blueprint files for scanning benchmarks."""
    bp_dir = tmp_path / "blueprints"
    bp_dir.mkdir()
    for domain in ("automation", "script", "template"):
        domain_dir = bp_dir / domain
        domain_dir.mkdir(parents=True, exist_ok=True)
        for i in range(10):
            fpath = domain_dir / f"blueprint_{i}.yaml"
            fpath.write_text(
                textwrap.dedent(f"""\
                    blueprint:
                      name: Blueprint {i} ({domain})
                      domain: {domain}
                      source_url: https://github.com/user/{domain}_bp_{i}.yaml
                      input:
                        entity_{i}:
                          name: Entity {i}
                          selector:
                            entity:
                              domain: light
                    trigger:
                      platform: state
                    action:
                      - service: light.turn_on
                    mode: single
                """)
            )
    return bp_dir


@pytest.mark.benchmark
class TestHashComputation:
    """Benchmark hash-related hot paths (BP-3, AI-4)."""

    def test_hash_content_normalized(self, benchmark, sample_blueprint_content):
        """Benchmark _hash_content with normalization."""
        result = benchmark(
            BlueprintUpdateCoordinator._hash_content,
            sample_blueprint_content,
        )
        assert len(result) == 64

    def test_hash_content_already_normalized(self, benchmark, sample_blueprint_content):
        """Benchmark _hash_content with already_normalized=True."""
        result = benchmark(
            BlueprintUpdateCoordinator._hash_content,
            sample_blueprint_content,
            already_normalized=True,
        )
        assert len(result) == 64

    def test_hash_content_with_source_url(self, benchmark, sample_blueprint_content):
        """Benchmark _hash_content with source_url injection (full normalization)."""
        source_url = "https://github.com/home-assistant/core/blob/dev/blueprints/motion_light.yaml"

        def _hash_with_url():
            return BlueprintUpdateCoordinator._hash_content(
                sample_blueprint_content, source_url=source_url
            )

        result = benchmark(_hash_with_url)
        assert len(result) == 64

    def test_normalize_content_fast_path(self, benchmark, sample_blueprint_content):
        """Benchmark _normalize_content on already-normalized content."""
        result = benchmark(
            BlueprintUpdateCoordinator._normalize_content,
            sample_blueprint_content,
        )
        assert isinstance(result, str)

    def test_normalize_content_bom(self, benchmark):
        """Benchmark _normalize_content with BOM prefix."""
        content = "\ufeff" + "blueprint:\n  name: Test\n  source_url: https://example.com/bp.yaml\n"
        result = benchmark(
            BlueprintUpdateCoordinator._normalize_content,
            content,
        )
        assert not result.startswith("\ufeff")

    def test_normalize_content_crlf(self, benchmark):
        """Benchmark _normalize_content with CRLF line endings."""
        content = "blueprint:\r\n  name: Test\r\n  source_url: https://example.com/bp.yaml\r\n"
        result = benchmark(
            BlueprintUpdateCoordinator._normalize_content,
            content,
        )
        assert "\r" not in result

    def test_sha256_raw_baseline(self, benchmark, sample_blueprint_content):
        """Benchmark raw sha256 for baseline comparison."""
        encoded = sample_blueprint_content.encode("utf-8")
        result = benchmark(lambda: hashlib.sha256(encoded).hexdigest())
        assert len(result) == 64


@pytest.mark.benchmark
class TestYamlProcessing:
    """Benchmark YAML parsing and semantic normalization (BP-1, AI-2)."""

    def test_ensure_source_url_normal(self, benchmark, sample_blueprint_content):
        """Benchmark _ensure_source_url with standard blueprint."""
        self._run_url_benchmark(
            "https://github.com/home-assistant/core/blob/dev/blueprint.yaml",
            benchmark,
            sample_blueprint_content,
        )

    def test_ensure_source_url_large(self, benchmark, sample_blueprint_content_large):
        """Benchmark _ensure_source_url with large blueprint (50 inputs)."""
        self._run_url_benchmark(
            "https://github.com/example/large.yaml",
            benchmark,
            sample_blueprint_content_large,
        )

    def _run_url_benchmark(self, source_url: str, benchmark, blueprint_content: str) -> None:
        """Helper to run the URL benchmark with validation assertions."""
        result = benchmark(
            BlueprintUpdateCoordinator._ensure_source_url, blueprint_content, source_url
        )
        assert source_url in result

    def test_parse_yaml_baseline(self, benchmark, sample_blueprint_content):
        """Benchmark raw yaml_util.parse_yaml for baseline."""
        result = benchmark(yaml_util.parse_yaml, sample_blueprint_content)
        assert isinstance(result, dict)

    def test_yaml_dump_baseline(self, benchmark, sample_blueprint_content):
        """Benchmark yaml_util.dump for baseline."""
        parsed = yaml_util.parse_yaml(sample_blueprint_content)

        def _dump():
            return yaml_util.dump(cast(dict[str, Any], parsed))

        result = benchmark(_dump)
        assert isinstance(result, str)

    def test_stabilize_yaml_structure(self, benchmark, sample_blueprint_content):
        """Benchmark _stabilize_yaml_structure recursive merge."""
        parsed = yaml_util.parse_yaml(sample_blueprint_content)

        def _stabilize():
            return BlueprintUpdateCoordinator._stabilize_yaml_structure(parsed, parsed)

        result = benchmark(_stabilize)
        assert isinstance(result, (dict, OrderedDict))

    def test_get_blueprint_block(self, benchmark, sample_blueprint_content):
        """Benchmark _get_blueprint_block extraction."""
        result = benchmark(
            BlueprintUpdateCoordinator._get_blueprint_block,
            "test.yaml",
            content=sample_blueprint_content,
        )
        assert result is not None
        assert "name" in result

    def test_parse_blueprint_data(self, benchmark, sample_blueprint_content):
        """Benchmark _parse_blueprint_data full extraction."""
        result = benchmark(
            BlueprintUpdateCoordinator._parse_blueprint_data,
            "test.yaml",
            sample_blueprint_content,
            "automation/test.yaml",
        )
        assert result is not None
        assert "local_hash" in result
        assert "name" in result
        assert "domain" in result
        assert "source_url" in result


@pytest.mark.benchmark
class TestSchemaExtraction:
    """Benchmark input schema extraction (BP-2)."""

    def test_extract_inputs_schema_standard(self, benchmark, sample_blueprint_content):
        """Benchmark _extract_inputs_schema with standard blueprint."""
        schema = self._run_schema_benchmark(
            benchmark, sample_blueprint_content, "motion_entity", "light_target"
        )
        assert "no_motion_wait" in schema

    def test_extract_inputs_schema_nested(self, benchmark, sample_blueprint_content_nested):
        """Benchmark _extract_inputs_schema with nested sections (HA 2024.6+)."""
        schema = self._run_schema_benchmark(
            benchmark, sample_blueprint_content_nested, "sensor_a", "sensor_b"
        )
        assert schema["sensor_b"]["mandatory"] is False

    def _run_schema_benchmark(self, benchmark, blueprint_content, input_key_a, input_key_b):
        """Helper to run the schema extraction benchmark with validation assertions."""
        result, error = benchmark(
            BlueprintUpdateCoordinator._extract_inputs_schema, blueprint_content
        )
        assert error is None
        assert input_key_a in result
        assert input_key_b in result
        return result

    def test_extract_inputs_schema_large(self, benchmark, sample_blueprint_content_large):
        """Benchmark _extract_inputs_schema with 50 inputs."""
        schema, error = benchmark(
            BlueprintUpdateCoordinator._extract_inputs_schema,
            sample_blueprint_content_large,
        )
        assert error is None
        assert len(schema) == 50


@pytest.mark.benchmark
class TestRiskDetection:
    """Benchmark breaking-change risk detection algorithms."""

    @property
    def _old_schema(self) -> dict[str, Any]:
        """Return a sample old blueprint input schema with optional fields."""
        return {
            "motion_entity": {"mandatory": True, "selector": "entity"},
            "light_target": {"mandatory": True, "selector": "target"},
            "no_motion_wait": {"mandatory": False, "selector": "number"},
        }

    @property
    def _new_schema(self) -> dict[str, Any]:
        """Return a sample new blueprint input schema with an added mandatory field."""
        return {
            "motion_entity": {"mandatory": True, "selector": "entity"},
            "light_target": {"mandatory": True, "selector": "target"},
            "no_motion_wait": {"mandatory": True, "selector": "number"},
            "new_input": {"mandatory": True, "selector": "entity"},
        }

    @property
    def _entity_configs(self) -> dict[str, dict[str, Any]]:
        """Return sample entity configurations for risk detection benchmarks."""
        return {
            "automation.motion_light_hallway": {
                "motion_entity": "binary_sensor.hallway_motion",
                "light_target": "light.hallway",
            },
            "automation.motion_light_kitchen": {
                "motion_entity": "binary_sensor.kitchen_motion",
                "light_target": "light.kitchen",
                "no_motion_wait": 60,
            },
        }

    def test_detect_new_mandatory_inputs(self, benchmark):
        """Benchmark _detect_new_mandatory_inputs."""
        result = benchmark(
            BlueprintUpdateCoordinator._detect_new_mandatory_inputs,
            self._old_schema,
            self._new_schema,
        )
        assert len(result) >= 1
        assert any(r["args"]["input"] == "new_input" for r in result)

    def test_detect_missing_inputs(self, benchmark):
        """Benchmark _detect_missing_inputs."""
        result = benchmark(
            BlueprintUpdateCoordinator._detect_missing_inputs,
            self._new_schema,
            self._entity_configs,
        )
        assert isinstance(result, list)

    def test_detect_breaking_changes_components(self, benchmark, sample_blueprint_content):
        """Benchmark _detect_new_mandatory_inputs + _detect_missing_inputs combined."""
        new_content = textwrap.dedent("""\
            blueprint:
              name: Motion-activated Light v2
              domain: automation
              source_url: https://github.com/updated/blueprint.yaml
              input:
                motion_entity:
                  name: Motion Sensor
                  selector:
                    entity:
                      domain: binary_sensor
                light_target:
                  name: Light
                  selector:
                    entity:
                      domain: light
                no_motion_wait:
                  name: Wait time
                  default: 120
                  selector:
                    number:
                      min: 0
                      max: 3600
                new_mandatory_input:
                  name: Required New Field
                  selector:
                    entity:
        """)
        new_schema, _ = BlueprintUpdateCoordinator._extract_inputs_schema(new_content)

        def _run():
            risks = []
            risks.extend(
                BlueprintUpdateCoordinator._detect_new_mandatory_inputs(
                    self._old_schema, new_schema
                )
            )
            risks.extend(
                BlueprintUpdateCoordinator._detect_missing_inputs(new_schema, self._entity_configs)
            )
            return BlueprintUpdateCoordinator._dedupe_risks(risks)

        result = benchmark(_run)
        assert isinstance(result, list)

    def test_dedupe_risks_small(self, benchmark):
        """Benchmark _dedupe_risks with a small list (3 items, 2 unique)."""
        risks: list[StructuredRisk] = [
            {"type": BlueprintRiskType.NEW_MANDATORY, "args": {"input": "sensor_a"}},
            {"type": BlueprintRiskType.NEW_MANDATORY, "args": {"input": "sensor_a"}},
            {
                "type": BlueprintRiskType.MISSING_INPUT,
                "args": {"entity": "automation.x", "input": "k"},
            },
        ]
        result = benchmark(BlueprintUpdateCoordinator._dedupe_risks, risks)
        assert len(result) == 2

    def test_dedupe_risks_large(self, benchmark):
        """Benchmark _dedupe_risks with a large list (1000 items, 100 unique)."""
        risks: list[StructuredRisk] = []
        for i in range(100):
            risk: StructuredRisk = {
                "type": BlueprintRiskType.NEW_MANDATORY,
                "args": {"input": f"input_{i}"},
            }
            risks.extend([risk] * 10)

        result = benchmark(BlueprintUpdateCoordinator._dedupe_risks, risks)
        assert len(result) == 100


@pytest.mark.benchmark
class TestProviderPerformance:
    """Benchmark provider operations (BP-4, AI-1)."""

    @property
    def _test_urls(self) -> list[str]:
        """Return a diverse set of test URLs covering all provider types."""
        return [
            "https://github.com/home-assistant/core/blob/dev/blueprints/motion_light.yaml",
            "https://raw.githubusercontent.com/home-assistant/core/dev/blueprints/test.yaml",
            "https://gist.github.com/user/abc123def456",
            "https://gist.github.com/user/abc123def456/raw",
            "https://community.home-assistant.io/t/some-blueprint/12345",
            "https://gitlab.com/user/repo/-/blob/main/blueprint.yaml",
            "https://codeberg.org/user/repo/src/branch/main/blueprint.yaml",
            "https://bitbucket.org/user/repo/src/main/blueprint.yaml",
            "https://example.com/raw/blueprint.yaml",
        ]

    def test_provider_registry_lookup(self, benchmark):
        """Benchmark provider registry lookup across all providers."""
        urls = self._test_urls

        def _lookup_all():
            results = []
            for u in urls:
                results.append(registry.get_provider(u))
            return results

        results = benchmark(_lookup_all)
        assert all(r is not None for r in results)

    def test_github_normalize_url(self, benchmark):
        """Benchmark GitHubProvider.normalize_url."""
        provider = GitHubProvider()
        url = "https://github.com/home-assistant/core/blob/dev/blueprints/motion_light.yaml"
        result = benchmark(provider.normalize_url, url)
        assert result is not None
        assert urlparse(result).hostname == "raw.githubusercontent.com"

    def test_haforum_parse_content(self, benchmark):
        """Benchmark HAForumProvider.parse_content with JSON data."""
        provider = HAForumProvider()
        json_data = {
            "post_stream": {
                "posts": [
                    {
                        "username": "author",
                        "cooked": (
                            "<p>Some text</p><code>blueprint:\n  name: Test\n"
                            "  domain: automation\n  source_url: https://example.com/bp.yaml\n"
                            "</code>"
                        ),
                    }
                ]
            },
            "slug": "test-blueprint",
        }
        result = benchmark(provider.parse_content, "", json_data)
        assert result is not None
        assert "blueprint:" in result

    def test_replace_path_segment(self, benchmark):
        """Benchmark _replace_path_segment helper for GitLab/Codeberg/Bitbucket."""
        url = "https://gitlab.com/user/repo/-/blob/main/blueprint.yaml"
        result = benchmark(_replace_path_segment, url, "/-/raw/", "/-/blob/", "/-/raw/")
        assert "/-/raw/" in result

    def test_all_providers_can_handle_scan(self, benchmark):
        """Benchmark scanning all providers against a set of URLs."""
        registry_obj = ProviderRegistry()
        urls = self._test_urls

        def _scan():
            results: dict[str, dict[str, bool]] = {}
            for u in urls:
                results[u] = {
                    provider.provider_type.value: provider.can_handle(u)
                    for provider in registry_obj
                }
            return results

        result = benchmark(_scan)
        assert len(result) == len(urls)

    def test_normalize_url_full_chain(self, benchmark):
        """Benchmark the full normalize_url chain from utils.py."""
        url = "https://github.com/home-assistant/core/blob/dev/blueprint.yaml"
        result = benchmark(normalize_url, url)
        assert urlparse(result).hostname == "raw.githubusercontent.com"


@pytest.mark.benchmark
class TestDataStructureOperations:
    """Benchmark data structure operations in coordinator (BP-6, AI-5)."""

    def test_dict_build_with_get(self, benchmark):
        """Benchmark dict building with .get() pattern (current style)."""
        data = {
            f"path_{i}": {
                "name": f"Name {i}",
                "relative_path": f"automation/bp_{i}.yaml",
                "domain": "automation",
                "source_url": f"https://example.com/bp_{i}.yaml",
                "local_hash": f"hash_{i}",
                "updatable": i % 2 == 0,
                "remote_hash": f"remote_hash_{i}" if i % 2 == 0 else None,
                "last_error": None,
                "etag": f"etag_{i}" if i % 3 == 0 else None,
                "last_modified": None,
                "backups_count": i % 5,
                "breaking_risks": [],
                "update_blocking_reason": None,
                "auto_update_last_error": None,
                "invalid_remote_hash": None,
                "remote_content": None,
                "provider_type": "github",
            }
            for i in range(100)
        }

        def _build_with_get():
            result = {}
            for path, info in data.items():
                result[path] = {
                    "name": info.get("name"),
                    "relative_path": info.get("relative_path"),
                    "domain": info.get("domain"),
                    "source_url": info.get("source_url"),
                    "local_hash": info.get("local_hash"),
                    "updatable": info.get("updatable", False),
                    "remote_hash": info.get("remote_hash"),
                    "last_error": info.get("last_error"),
                    "etag": info.get("etag"),
                }
            return result

        r1 = benchmark(_build_with_get)
        assert len(r1) == 100

    def test_should_include_blueprint_whitelist(self, benchmark):
        """Benchmark should_include_blueprint with whitelist filter."""
        selected = {f"automation/bp_{i}.yaml" for i in range(50)}

        def _filter():
            results = []
            for i in range(200):
                rel_path = f"automation/bp_{i}.yaml"
                results.append(should_include_blueprint(rel_path, "whitelist", selected))
            return results

        result = benchmark(_filter)
        assert len(result) == 200

    def test_metadata_merge_single_pass(self, benchmark):
        """Benchmark the metadata merge pattern from _async_save_metadata."""
        persisted = {
            f"automation/bp_{i}.yaml": {"etag": f"e{i}", "remote_hash": f"rh{i}"}
            for i in range(200)
        }
        current_data = {
            f"path_{i}": {
                "relative_path": f"automation/bp_{i}.yaml",
                "remote_hash": f"new_rh{i}" if i % 2 else None,
                "etag": f"new_e{i}" if i % 3 else None,
                "last_modified": f"lm{i}" if i % 4 else None,
                "source_url": f"https://example.com/bp_{i}.yaml",
            }
            for i in range(200)
        }

        def _merge():
            current_map = {
                i["relative_path"]: i for i in current_data.values() if i.get("relative_path")
            }
            candidate = {
                rel: dict(persisted.get(rel, {})) for rel in sorted({*persisted, *current_map})
            }
            for rel in candidate:
                if info := current_map.get(rel):
                    existing = candidate[rel]
                    for field in ("remote_hash", "etag", "last_modified", "source_url"):
                        if val := info.get(field):
                            existing[field] = val
            return candidate

        result = benchmark(_merge)
        assert len(result) == 200


@pytest.mark.benchmark
class TestBackupOperations:
    """Benchmark backup-related operations (EC-5)."""

    def test_count_backups_repeated(self, benchmark, tmp_path):
        """Benchmark _count_backups_sync_helper repeated calls."""
        file_path = str(tmp_path / "test.yaml")
        for i in range(1, 6):
            (tmp_path / f"test.yaml.bak.{i}").write_text(f"backup_{i}")

        _count_backups_sync_helper(file_path, 10)

        result = benchmark(_count_backups_sync_helper, file_path, 10)
        assert result == 5

    def test_count_backups_initial(self, benchmark, tmp_path):
        """Benchmark _count_backups_sync_helper initial call."""
        file_path = str(tmp_path / "test.yaml")
        for i in range(1, 6):
            (tmp_path / f"test.yaml.bak.{i}").write_text(f"backup_{i}")

        def _cold():
            return _count_backups_sync_helper(file_path, 10)

        result = benchmark(_cold)
        assert result == 5

    def test_rotate_backups(self, benchmark, tmp_path):
        """Benchmark _rotate_backups with existing backups."""
        file_path = str(tmp_path / "test.yaml")

        def _rotate():
            (tmp_path / "test.yaml").write_text("current_content")
            for i in range(1, 4):
                (tmp_path / f"test.yaml.bak.{i}").write_text(f"old_{i}")
            BlueprintUpdateCoordinator._rotate_backups(file_path, 5)
            for i in range(1, 6):
                bak = tmp_path / f"test.yaml.bak.{i}"
                if bak.exists():
                    bak.unlink()

        benchmark(_rotate)


@pytest.mark.benchmark
class TestUtilityFunctions:
    """Benchmark utility functions."""

    def test_redact_url(self, benchmark):
        """Benchmark redact_url with credentials, query, fragment."""
        url = "https://user:pass@github.com/user/repo/blob/dev/blueprint.yaml?token=abc#frag"
        result = benchmark(redact_url, url)
        assert "pass" not in result
        assert "token" not in result

    def test_sanitize_error_detail(self, benchmark):
        """Benchmark sanitize_error_detail with long URL-containing message."""
        detail = (
            "Very long error message with URL https://example.com/path and | pipe characters " * 5
        )
        result = benchmark(sanitize_error_detail, detail, max_length=120)
        assert len(result) <= 123
        assert "|" not in result

    def test_normalize_domain(self, benchmark):
        """Benchmark normalize_domain with mixed inputs."""

        def _normalize():
            results = []
            for domain in (
                "automation",
                "script",
                "template",
                "Automation",
                "  script  ",
                "unknown",
            ):
                results.append(normalize_domain(domain))
            return results

        result = benchmark(_normalize)
        assert len(result) == 6

    def test_get_config_value(self, benchmark):
        """Benchmark get_config_value with ConfigEntry-like object."""

        class FakeEntry:
            """Minimal ConfigEntry-like object with options dict for benchmarks."""

            options: ClassVar[dict[str, str | int]] = {"key1": "val1", "key2": 42}

        entry = FakeEntry()

        def _get():
            results = []
            for _ in range(100):
                results.append(get_config_value(entry, "key1", "default"))
                results.append(get_config_value(entry, "missing", "default"))
            return results

        result = benchmark(_get)
        assert len(result) == 200

    def test_get_validated_filter_mode(self, benchmark):
        """Benchmark get_validated_filter_mode with valid and invalid inputs."""

        def _validate():
            results = []
            for mode in (
                FILTER_MODE_ALL,
                FILTER_MODE_WHITELIST,
                FILTER_MODE_BLACKLIST,
                "invalid",
                None,
                42,
            ):
                results.append(get_validated_filter_mode(mode))
            return results

        result = benchmark(_validate)
        assert len(result) == 6

    def test_get_validated_selected_blueprints(self, benchmark):
        """Benchmark get_validated_selected_blueprints with mixed types."""

        def _validate():
            results = []
            for sel in (["bp1.yaml", "bp2.yaml"], "bp1.yaml", None, {"bad": "type"}, 123):
                results.append(get_validated_selected_blueprints(sel))
            return results

        result = benchmark(_validate)
        assert len(result) == 5


@pytest.mark.benchmark
class TestStringOperations:
    """Benchmark string and regex operations (AI-4, EC-7)."""

    def test_normalize_hostname(self, benchmark):
        """Benchmark _normalize_hostname with mixed cases."""

        def _normalize():
            results = []
            for host in (
                "GITHUB.COM",
                "www.github.com",
                "github.com",
                "RAW.githubusercontent.com",
                None,
                "",
            ):
                results.append(_normalize_hostname(host))
            return results

        result = benchmark(_normalize)
        assert len(result) == 6

    def test_urlparse_baseline(self, benchmark):
        """Benchmark urlparse overhead (used heavily in providers)."""
        url = "https://github.com/home-assistant/core/blob/dev/blueprints/motion_light.yaml"

        def _parse():
            return urlparse(url)

        result = benchmark(_parse)
        assert result.scheme == "https"


@pytest.mark.benchmark
class TestEndToEndMicro:
    """Micro end-to-end benchmarks for key coordinator flows."""

    def test_full_content_pipeline(self, benchmark, sample_blueprint_content):
        """Benchmark the full content processing pipeline for a single blueprint."""
        source_url = "https://github.com/home-assistant/core/blob/dev/blueprint.yaml"

        def _pipeline():
            local_hash = BlueprintUpdateCoordinator._hash_content(
                sample_blueprint_content, source_url
            )
            normalized = BlueprintUpdateCoordinator._ensure_source_url(
                sample_blueprint_content, source_url
            )
            remote_hash = BlueprintUpdateCoordinator._hash_content(
                normalized, source_url, already_normalized=True
            )
            updatable = local_hash != remote_hash
            schema, _ = BlueprintUpdateCoordinator._extract_inputs_schema(sample_blueprint_content)
            return updatable, len(schema)

        result = benchmark(_pipeline)
        assert result[0] is False

    def test_scan_like_operation(self, benchmark, blueprint_files_dir):
        """Benchmark a scan-like read operation on 30 blueprint files."""
        bp_dir = str(blueprint_files_dir)
        files = []
        for domain in ("automation", "script", "template"):
            domain_dir = os.path.join(bp_dir, domain)
            if os.path.isdir(domain_dir):
                for fname in os.listdir(domain_dir):
                    if fname.endswith((".yaml", ".yml")):
                        files.append(os.path.join(domain_dir, fname))

        def _scan():
            results = {}
            for fpath in files:
                with open(fpath, encoding="utf-8") as f:
                    content = f.read()
                rel = os.path.relpath(fpath, bp_dir).replace("\\", "/")
                parsed = BlueprintUpdateCoordinator._parse_blueprint_data(fpath, content, rel)
                if parsed:
                    results[fpath] = parsed
            return results

        result = benchmark(_scan)
        assert len(result) == 30

    def test_translation_lookup_pattern(self, benchmark):
        """Benchmark the translation lookup pattern from async_translate (BP-7, AI-3)."""
        translations = {
            "component.blueprints_updater.common.update_available": (
                "Update available for {source_url}"
            ),
            "component.blueprints_updater.common.up_to_date": "Up to date",
            "component.blueprints_updater.exceptions.missing_url": "URL is required",
            "component.blueprints_updater.exceptions.unsafe_url": "Unsafe URL: {url}",
            "component.blueprints_updater.selector.filter_mode": "",
            "component.blueprints_updater.services.import_blueprint": "",
        }
        search_categories = [
            "common",
            "exceptions",
            "selector",
            "title",
            "config",
            "options",
            "services",
            "entity",
            "device",
            "device_automation",
            "entity_component",
            "issues",
        ]

        def _lookup():
            for _ in range(100):
                for cat in search_categories:
                    full_key = f"component.blueprints_updater.{cat}.test_key"
                    _ = translations.get(f"{full_key}.message") or translations.get(full_key)
            return True

        result = benchmark(_lookup)
        assert result is True

    def test_get_vs_setdefault_pattern(self, benchmark):
        """Benchmark .get() vs .setdefault() for data initialization."""

        def _pattern_get():
            d: dict[str, Any] = {}
            for _ in range(100):
                _ = d.get("key", {})
            return d

        r1 = benchmark(_pattern_get)
        assert isinstance(r1, dict)


@pytest.mark.benchmark
class TestConcurrencyPatterns:
    """Benchmark locking and concurrency patterns (BP-5)."""

    def test_pacing_lock_contention(self, benchmark):
        """Benchmark the pacing lock pattern under contention."""

        async def _paced(lock: asyncio.Lock, last_time: list[float], _i: int) -> None:
            """Simulate a paced task that enforces a minimum interval between operations."""
            async with lock:
                now = time.monotonic()
                delay = max(0.0, 0.5 - (now - last_time[0]))
                last_time[0] = now + delay
            if delay:
                await asyncio.sleep(delay)

        async def _async_run() -> None:
            """Create and gather 20 paced tasks under a shared lock."""
            lock = asyncio.Lock()
            last_time = [0.0]
            tasks = [_paced(lock, last_time, i) for i in range(20)]
            await asyncio.gather(*tasks)

        def _run() -> None:
            asyncio.run(_async_run())

        benchmark(_run)

    def test_double_checked_lock(self, benchmark):
        """Benchmark the double-checked locking pattern used in _is_safe_url."""

        async def _check(cache: dict[str, bool], lock: asyncio.Lock, hostname: str) -> bool:
            """Perform a double-checked lock pattern: fast path, then lock, then re-check."""
            if hostname in cache:
                return cache[hostname]
            async with lock:
                if hostname in cache:
                    return cache[hostname]
                cache[hostname] = True
                return True

        async def _async_run() -> None:
            """Create and gather 50 double-checked lock tasks across 5 unique keys."""
            cache: dict[str, bool] = {}
            lock = asyncio.Lock()
            tasks = [_check(cache, lock, f"host{i % 5}.com") for i in range(50)]
            await asyncio.gather(*tasks)

        def _run() -> None:
            asyncio.run(_async_run())

        benchmark(_run)


@pytest.mark.benchmark
class TestMemoryAllocation:
    """Benchmark memory allocation patterns."""

    def test_large_dict_creation(self, benchmark):
        """Benchmark creating large result dictionaries (like coordinator.data)."""

        def _create():
            return {
                f"path_{i}": {
                    "name": f"Name {i}",
                    "relative_path": f"automation/bp_{i}.yaml",
                    "domain": "automation",
                    "source_url": f"https://example.com/bp_{i}.yaml",
                    "local_hash": "a" * 64,
                    "updatable": False,
                    "remote_hash": None,
                    "invalid_remote_hash": None,
                    "remote_content": None,
                    "last_error": None,
                    "etag": None,
                    "last_modified": None,
                    "persisted_source_url": None,
                    "backups_count": 0,
                    "breaking_risks": [],
                    "update_blocking_reason": None,
                    "auto_update_last_error": None,
                    "provider_type": "github",
                }
                for i in range(100)
            }

        result = benchmark(_create)
        assert len(result) == 100

    def test_list_dedup_dict_fromkeys(self, benchmark):
        """Benchmark list(dict.fromkeys(...)) vs set-based dedup."""
        items = ["a", "b", "c", "a", "b", "d", "e", "f"] * 125

        def _dedup():
            return list(dict.fromkeys(items))

        result = benchmark(_dedup)
        assert len(result) == 6
