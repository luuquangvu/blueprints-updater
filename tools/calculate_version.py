"""Version calculation logic for the release workflow.

This script calculates the next version based on the selected bump type
(major, minor, patch) and pre-release flag, ensuring Semantic Versioning 2.0.0
compliance and branch-aware regression checking.
"""

import os
import re
import sys

from packaging.version import parse


def main() -> None:
    """Calculate the next version based on environment variables."""
    bump_type = os.environ["BUMP_TYPE"]
    is_prerelease = os.environ["IS_PRERELEASE"].lower() == "true"
    latest_stable_str = os.environ["LATEST_STABLE"]
    current_any_str = os.environ["CURRENT_ANY"]
    all_tags = os.environ["ALL_TAGS"].split("\n")

    prefix = "v" if latest_stable_str.startswith("v") else ""

    latest_stable_numeric = latest_stable_str.lstrip("v")
    v = [int(x) for x in latest_stable_numeric.split(".")]
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
