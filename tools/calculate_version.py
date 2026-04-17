"""Version calculation utility for Semantic Versioning 2.0.0 releases.

This module provides a standalone CLI tool to compute the next valid version
based on repository history and user-selected bump strategies. It enforces
branch-aware regression checks and handles pre-release (RC) increments by
scanning all reachable tags across both 'v'-prefixed and numeric-only formats.

This utility is specifically designed for use within GitHub Actions release
workflows to ensure consistent versioning across project branches.
"""

import os
import re
import sys

from packaging.version import parse


def main() -> None:
    """Compute and print the next version using environment configuration.

    The function orchestrates the full calculation pipeline:
    1. Parses environment variables for bump strategies and base versions.
    2. Validates input formats (X.Y.Z).
    3. Increments specific version segments or RC counters.
    4. Performs regression checks against stable and latest reachable tags.

    Environment Inputs:
        BUMP_TYPE: Version segment to increment ('major', 'minor', 'patch').
        IS_PRERELEASE: Boolean string ('true' or 'false') for RC suffixes.
        LATEST_STABLE: Baseline stable version string (e.g., 'v1.0.2').
        CURRENT_ANY: Latest reachable tag for regression checks.
        ALL_TAGS: Newline-separated tags for exhaustive pre-release scanning.

    Output:
        Prints the calculated version string to standard output.
        Exits with status 1 on validation failure or malformed input.
    """
    bump_type = os.environ["BUMP_TYPE"]
    is_prerelease = os.environ["IS_PRERELEASE"].lower() == "true"
    latest_stable_str = os.environ["LATEST_STABLE"]
    current_any_str = os.environ["CURRENT_ANY"]
    all_tags = os.environ["ALL_TAGS"].split("\n")

    prefix = "v" if latest_stable_str.startswith("v") else ""

    latest_stable_numeric = latest_stable_str.lstrip("v")
    parts = latest_stable_numeric.split(".")
    if len(parts) != 3:
        print(
            f"Error: Invalid version format '{latest_stable_str}', expected X.Y.Z",
            file=sys.stderr,
        )
        sys.exit(1)

    if bump_type not in ("major", "minor", "patch"):
        print(
            f"Error: Invalid bump_type '{bump_type}', expected major, minor, or patch",
            file=sys.stderr,
        )
        sys.exit(1)

    v = [int(parts[0]), int(parts[1]), int(parts[2])]
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
        result = f"{prefix}{target_stable}"
    else:
        rc_pattern = re.compile(f"^v?{re.escape(target_stable)}" + r"-rc\.(\d+)$")
        rc_numbers = []
        for tag in all_tags:
            if match := rc_pattern.match(tag.strip()):
                rc_numbers.append(int(match[1]))

        next_rc = max(rc_numbers) + 1 if rc_numbers else 1
        result = f"{prefix}{target_stable}-rc.{next_rc}"

    if parse(result) <= parse(latest_stable_str):
        print(
            f"Error: Calculated version {result} is not greater than "
            f"latest stable {latest_stable_str}",
            file=sys.stderr,
        )
        sys.exit(1)

    if (
        is_prerelease
        and current_any_str.lstrip("v").startswith(target_stable)
        and parse(result) <= parse(current_any_str)
    ):
        print(
            f"Error: Calculated pre-release {result} is not greater than "
            f"latest version {current_any_str}",
            file=sys.stderr,
        )
        sys.exit(1)

    print(result)


if __name__ == "__main__":
    main()
