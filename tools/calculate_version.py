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


def normalize_version(version_str: str, prefix: str) -> str:
    """Strip prefixes and return a clean version string for parsing.

    Args:
        version_str: The version string to normalize.
        prefix: The primary prefix to remove.

    Returns:
        A normalized Semantic Version string.
    """
    normalized = version_str.removeprefix(prefix)
    return normalized.removeprefix("v")


def main() -> None:
    """Compute and print the next version using environment configuration.

    The function orchestrates the full calculation pipeline:
    1. Parses environment variables for bump strategies and base versions.
    2. Validates input formats using packaging.version.
    3. Increments specific version segments or RC counters.
    4. Performs regression checks against stable and latest reachable tags.

    Environment Inputs:
        BUMP_TYPE: Version segment to increment ('major', 'minor', 'patch').
        IS_PRERELEASE: Boolean string ('true' or 'false') for RC suffixes.
        LATEST_STABLE: Baseline stable version string (e.g., 'v1.0.2').
        CURRENT_ANY: Latest reachable tag for regression checks.
        ALL_TAGS: Newline-separated tags for exhaustive pre-release scanning.
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

    latest_stable_str = os.environ["LATEST_STABLE"]
    current_any_str = os.environ["CURRENT_ANY"]
    all_tags_raw = os.environ.get("ALL_TAGS", "")
    all_tags = [t.strip() for t in all_tags_raw.split("\n") if t.strip()]

    detected_prefix = "v" if latest_stable_str.startswith("v") else ""
    prefix = os.environ.get("TAG_PREFIX", detected_prefix)

    try:
        baseline = normalize_version(latest_stable_str, prefix)
        parsed = parse(baseline)
        v = [parsed.major, parsed.minor, parsed.micro]
    except Exception as e:
        print(
            f"Error: Could not parse version '{latest_stable_str}': {e}",
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
    norm_result = parse(normalize_version(result_str, prefix))
    norm_latest = parse(normalize_version(latest_stable_str, prefix))
    norm_current = parse(normalize_version(current_any_str, prefix))

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


def _calculate_next_rc(prefix: str, target_stable: str, all_tags: list[str]) -> str:
    p_regex = f"({re.escape(prefix)}|v)?" if prefix != "v" else "v?"
    rc_pattern = re.compile(rf"^{p_regex}{re.escape(target_stable)}-rc\.(\d+)$")

    rc_numbers = []
    for tag in all_tags:
        if match := rc_pattern.match(tag.strip()):
            rc_numbers.append(int(match[1]))

    next_rc = max(rc_numbers) + 1 if rc_numbers else 1
    return f"{prefix}{target_stable}-rc.{next_rc}"


if __name__ == "__main__":
    main()
