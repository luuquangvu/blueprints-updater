"""Version calculation utility for Semantic Versioning 2.0.0 releases.

This module provides a standalone CLI tool to compute the next valid version
based on repository history and user-selected bump strategies. It enforces
global regression checks and handles pre-release (RC) increments by
scanning all reachable tags across custom prefixes and standard formats.

This utility is specifically designed for use within GitHub Actions release
workflows to ensure consistent versioning across project branches.
"""

import os
import re
import sys
from dataclasses import dataclass
from typing import NoReturn

from packaging.version import parse

DEFAULT_VERSION = "0.0.0"
DEFAULT_PREFIX = "v"


def _normalize_version(version_str: str, prefix: str) -> str:
    """Strip prefixes and return a clean version string for parsing.

    The function enforces consistency: if a version string does not start
    with the expected prefix (or 'v' as a fallback), it raises an error
    to prevent style contamination.

    Args:
        version_str: The version string to normalize.
        prefix: The primary prefix to remove.

    Returns:
        A normalized Semantic Version string.

    Raises:
        ValueError: If the version string has an unexpected prefix.
    """
    if not version_str or version_str == DEFAULT_VERSION:
        return DEFAULT_VERSION

    if prefix and version_str.startswith(prefix):
        normalized = version_str.removeprefix(prefix)
    elif version_str.startswith("v"):
        normalized = version_str.removeprefix("v")
    elif prefix:
        raise ValueError(f"Version '{version_str}' does not match prefix '{prefix}' or 'v'")

    else:
        normalized = version_str
    return normalized


def _calculate_next_rc(
    prefix: str, target_stable: str, all_tags: list[str], is_auto_detected: bool
) -> str:
    """Calculate the next RC tag for a given stable target with prefix strictness.

    The function is strict about mismatches between the configured `prefix`
    and discovered tags:
    - If `prefix == "v"`, only 'v'-prefixed RC tags are considered.
    - If `prefix == ""`, both bare and 'v'-prefixed tags are allowed.
    - In auto-detection mode (is_auto_detected=True), mixing styles triggers
      an error to enforce repository consistency.
    - If any other `prefix` is used, only tags with that prefix are considered.

    Args:
        prefix: The configured TAG_PREFIX.
        target_stable: The base Semantic Version (e.g., '1.2.3').
        all_tags: List of existing tags to scan for RC patterns.
        is_auto_detected: Whether the prefix style was automatically detected.

    Returns:
        The calculated pre-release string (e.g., 'v1.2.3-rc.1').
    """
    if not prefix:
        v_regex = "v"
        bare_regex = ""
    elif prefix == "v":
        v_regex = "v"
        bare_regex = None
    else:
        v_regex = re.escape(prefix)
        bare_regex = None

    patterns: list[tuple[re.Pattern[str], str]] = [
        (re.compile(rf"^{v_regex}{re.escape(target_stable)}-rc\.(\d+)$"), prefix)
    ]

    if bare_regex is not None:
        patterns.append((re.compile(rf"^{bare_regex}{re.escape(target_stable)}-rc\.(\d+)$"), ""))

    rc_numbers: list[int] = []
    detected_prefixes: set[str] = set()

    for raw_tag in all_tags:
        tag = raw_tag.strip()
        for pattern, det_prefix in patterns:
            if match := pattern.fullmatch(tag):
                rc_numbers.append(int(match[1]))
                detected_prefixes.add(det_prefix)
                break

    if is_auto_detected and not prefix and len(detected_prefixes) > 1:
        print(
            f"Error: Inconsistent RC tag prefixes detected for {target_stable!r}: "
            "found both 'v' and unprefixed tags. Please standardize your tag format.",
            file=sys.stderr,
        )
        sys.exit(1)

    effective_prefix = prefix
    if not effective_prefix and detected_prefixes == {"v"}:
        effective_prefix = "v"

    next_rc = max(rc_numbers) + 1 if rc_numbers else 1
    return f"{effective_prefix}{target_stable}-rc.{next_rc}"


def _exit_with_error(message: str) -> NoReturn:
    """Print a version calculation error and exit."""
    print(f"Error: {message}", file=sys.stderr)
    raise SystemExit(1)


@dataclass(frozen=True, slots=True)
class _VersionInputs:
    """Environment inputs used to calculate a version."""

    bump_type: str
    is_prerelease: bool
    latest_stable: str
    current_any: str
    all_tags: list[str]
    prefix: str
    prefix_is_auto_detected: bool


def _version_inputs() -> _VersionInputs:
    """Parse version calculation inputs from the environment."""
    is_prerelease_env = os.environ.get("IS_PRERELEASE")
    if is_prerelease_env is None:
        _exit_with_error("Missing required environment variable IS_PRERELEASE")
    is_prerelease_raw = is_prerelease_env.strip().lower()
    if is_prerelease_raw not in ("true", "false"):
        _exit_with_error(
            f"Invalid IS_PRERELEASE value '{is_prerelease_env}', expected 'true' or 'false'"
        )
    bump_type_env = os.environ.get("BUMP_TYPE")
    if bump_type_env is None:
        _exit_with_error("Missing required environment variable BUMP_TYPE")
    latest_stable = os.environ.get("LATEST_STABLE", DEFAULT_VERSION)
    configured_prefix = os.environ.get("TAG_PREFIX")
    prefix = (
        configured_prefix
        if configured_prefix is not None
        else ("v" if latest_stable.startswith("v") else "")
    )
    return _VersionInputs(
        bump_type=bump_type_env,
        is_prerelease=is_prerelease_raw == "true",
        latest_stable=latest_stable,
        current_any=os.environ.get("CURRENT_ANY", DEFAULT_VERSION),
        all_tags=[tag.strip() for tag in os.environ.get("ALL_TAGS", "").split("\n") if tag.strip()],
        prefix=prefix,
        prefix_is_auto_detected=configured_prefix is None,
    )


def _target_stable_version(inputs: _VersionInputs) -> str:
    """Return the bumped stable version without a tag prefix."""
    try:
        stable_baseline = _normalize_version(inputs.latest_stable, inputs.prefix)
        parsed_stable = parse(stable_baseline)
        segments = [
            parsed_stable.major,
            parsed_stable.minor,
            parsed_stable.micro,
        ]
    except Exception as err:
        _exit_with_error(f"Could not parse baseline stable version '{inputs.latest_stable}': {err}")
    if inputs.bump_type not in ("major", "minor", "patch"):
        _exit_with_error(f"Invalid bump_type '{inputs.bump_type}', expected major, minor, or patch")
    if inputs.bump_type == "major":
        segments[0] += 1
        segments[1] = 0
        segments[2] = 0
    elif inputs.bump_type == "minor":
        segments[1] += 1
        segments[2] = 0
    else:
        segments[2] += 1
    return ".".join(str(segment) for segment in segments)


def _calculated_version(inputs: _VersionInputs, target_stable: str) -> str:
    """Return the stable or release-candidate version."""
    if not inputs.is_prerelease:
        return f"{inputs.prefix}{target_stable}"
    return _calculate_next_rc(
        inputs.prefix,
        target_stable,
        inputs.all_tags,
        inputs.prefix_is_auto_detected,
    )


def _verify_calculated_version(
    inputs: _VersionInputs,
    target_stable: str,
    result: str,
) -> None:
    """Verify that the calculated version advances repository history."""
    try:
        normalized_result = parse(_normalize_version(result, inputs.prefix))
        normalized_latest = parse(_normalize_version(inputs.latest_stable, inputs.prefix))
        normalized_current = parse(_normalize_version(inputs.current_any, inputs.prefix))
    except Exception as err:
        _exit_with_error(f"Verification parsing failed: {err}")
    if normalized_result <= normalized_latest:
        _exit_with_error(
            f"Calculated version {result} is not greater than latest stable {inputs.latest_stable}"
        )
    if (
        inputs.is_prerelease
        and normalized_current.base_version == target_stable
        and normalized_result <= normalized_current
    ):
        _exit_with_error(
            f"Calculated pre-release {result} is not greater than "
            f"latest version {inputs.current_any}"
        )


def main() -> None:
    """Compute, verify, and print the next configured version."""
    inputs = _version_inputs()
    target_stable = _target_stable_version(inputs)
    result = _calculated_version(inputs, target_stable)
    _verify_calculated_version(inputs, target_stable, result)
    print(result)


if __name__ == "__main__":
    main()
