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

from packaging.version import parse

DEFAULT_VERSION = "0.0.0"
DEFAULT_PREFIX = "v"


def normalize_version(version_str: str, prefix: str) -> str:
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


def _calculate_next_rc(prefix: str, target_stable: str, all_tags: list[str]) -> str:
    """Calculate the next RC tag for a given stable target with prefix strictness.

    The function is strict about mismatches between the configured `prefix`
    and discovered tags:
    - If `prefix == "v"`, only 'v'-prefixed RC tags are considered.
    - If `prefix == ""`, both bare and 'v'-prefixed tags are allowed, but
      mixing styles triggers an error to enforce repository consistency.
    - If any other `prefix` is used, only tags with that prefix are considered.

    Args:
        prefix: The configured TAG_PREFIX.
        target_stable: The base Semantic Version (e.g., '1.2.3').
        all_tags: List of existing tags to scan for RC patterns.

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
            if match := pattern.match(tag):
                rc_numbers.append(int(match[1]))
                detected_prefixes.add(det_prefix)
                break

    if not prefix and len(detected_prefixes) > 1:
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


def main() -> None:
    """Compute and print the next version using environment configuration.

    The function orchestrates the full calculation pipeline:
    1. Parses environment variables for bump strategies and base versions.
    2. Validates input formats and detects active version lines.
    3. Increments specific version segments or RC counters.
    4. Performs regression checks against stable and latest reachable tags.

    Environment Inputs:
        BUMP_TYPE: Version segment to increment ('major', 'minor', 'patch').
        IS_PRERELEASE: Boolean string ('true' or 'false') for RC suffixes.
        LATEST_STABLE: Baseline stable version string.
        CURRENT_ANY: Latest reachable tag for regression checks.
        ALL_TAGS: Newline-separated tags for exhaustive prerelease scanning.
        TAG_PREFIX: Optional override for the version prefix (e.g., 'v').

    Output:
        Prints the calculated version string to standard output.
        Exits with status 1 on validation failure or malformed input.
    """
    bump_type = os.environ["BUMP_TYPE"]

    is_prerelease_raw = os.environ["IS_PRERELEASE"].strip().lower()
    if is_prerelease_raw not in ("true", "false"):
        print(
            f"Error: Invalid IS_PRERELEASE value '{os.environ['IS_PRERELEASE']}', "
            "expected 'true' or 'false'",
            file=sys.stderr,
        )
        sys.exit(1)
    is_prerelease = is_prerelease_raw == "true"

    latest_stable_str = os.environ.get("LATEST_STABLE", DEFAULT_VERSION)
    current_any_str = os.environ.get("CURRENT_ANY", DEFAULT_VERSION)
    all_tags_raw = os.environ.get("ALL_TAGS", "")
    all_tags = [t.strip() for t in all_tags_raw.split("\n") if t.strip()]

    configured_prefix = os.environ.get("TAG_PREFIX")
    if configured_prefix is not None:
        prefix = configured_prefix
    else:
        prefix = "v" if latest_stable_str.startswith("v") else ""

    try:
        stable_baseline_str = normalize_version(latest_stable_str, prefix)
        parsed_stable = parse(stable_baseline_str)
        v = [parsed_stable.major, parsed_stable.minor, parsed_stable.micro]
    except Exception as e:
        print(
            f"Error: Could not parse baseline stable version '{latest_stable_str}': {e}",
            file=sys.stderr,
        )
        sys.exit(1)

    if bump_type not in ("major", "minor", "patch"):
        print(
            f"Error: Invalid bump_type '{bump_type}', expected major, minor, or patch",
            file=sys.stderr,
        )
        sys.exit(1)

    if bump_type == "major":
        v[0] += 1
        v[1] = 0
        v[2] = 0
    elif bump_type == "minor":
        v[1] += 1
        v[2] = 0
    else:
        v[2] += 1

    target_stable = f"{v[0]}.{v[1]}.{v[2]}"

    if not is_prerelease:
        result_str = f"{prefix}{target_stable}"
    else:
        result_str = _calculate_next_rc(prefix, target_stable, all_tags)

    try:
        norm_result = parse(normalize_version(result_str, prefix))
        norm_latest = parse(normalize_version(latest_stable_str, prefix))
        norm_current = parse(normalize_version(current_any_str, prefix))
    except Exception as e:
        print(f"Error: Verification parsing failed: {e}", file=sys.stderr)
        sys.exit(1)

    if norm_result <= norm_latest:
        print(
            f"Error: Calculated version {result_str} is not greater than "
            f"latest stable {latest_stable_str}",
            file=sys.stderr,
        )
        sys.exit(1)

    if (
        is_prerelease
        and normalize_version(current_any_str, prefix).startswith(target_stable)
        and norm_result <= norm_current
    ):
        print(
            f"Error: Calculated pre-release {result_str} is not greater than "
            f"latest version {current_any_str}",
            file=sys.stderr,
        )
        sys.exit(1)

    print(result_str)


if __name__ == "__main__":
    main()
