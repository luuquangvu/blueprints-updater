"""Update project metadata files with the new version.

This script updates manifest.json and pyproject.toml, ensuring that
Semantic Versioning is applied consistently across all project descriptors.
It performs safety checks to prevent overwriting dynamic versions.
"""

import json
import os
import sys

import tomlkit
from tomlkit.items import Table


def update_manifest(version: str) -> None:
    """Update the Home Assistant integration manifest.

    Args:
        version: The new version string to apply.
    """
    path = "custom_components/blueprints_updater/manifest.json"
    with open(path, encoding="utf-8") as f:
        manifest = json.load(f)

    manifest["version"] = version

    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")


def update_pyproject(version: str) -> None:
    """Update the pyproject.toml file.

    Args:
        version: The new version string to apply.
    """
    path = "pyproject.toml"
    with open(path, encoding="utf-8") as f:
        doc = tomlkit.parse(f.read())

    project = doc.get("project")
    if not isinstance(project, Table):
        print("Error: [project] must be a table in pyproject.toml", file=sys.stderr)
        sys.exit(1)

    dynamic = project.get("dynamic", [])
    if isinstance(dynamic, list) and "version" in dynamic:
        print(
            "Error: 'version' is declared as dynamic in pyproject.toml. Cannot update manually.",
            file=sys.stderr,
        )
        sys.exit(1)

    project["version"] = version

    with open(path, "w", encoding="utf-8") as f:
        f.write(tomlkit.dumps(doc))


def main() -> None:
    """Read the version from the environment and update all metadata files."""
    version = os.environ.get("NEW_VERSION")
    if not version:
        print("Error: NEW_VERSION environment variable is not set", file=sys.stderr)
        sys.exit(1)

    try:
        update_manifest(version)
        update_pyproject(version)
    except Exception as e:
        print(f"Error updating project metadata: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
