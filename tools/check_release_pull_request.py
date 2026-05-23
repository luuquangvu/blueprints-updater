"""Validate whether a merged pull request should publish a release."""

import json
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

VERSION_PATTERN = r"(([0-9]+\.[0-9]+\.[0-9]+)(?:-rc\.[0-9]+)?)"


@dataclass(frozen=True)
class ReleaseGateResult:
    """Release gate decision and outputs for GitHub Actions."""

    should_publish: bool
    message: str
    version: str = ""
    tag: str = ""
    prerelease: bool = False


def _normalized_version(value: Any) -> str | None:
    """Return a valid release version string or None."""
    if not isinstance(value, str):
        return None
    match = re.fullmatch(VERSION_PATTERN, value)
    return match.group(1) if match else None


def _read_manifest_version(manifest_path: Path) -> tuple[str, str | None]:
    """Read and validate the integration manifest version."""
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw_version = manifest.get("version", "")
    return str(raw_version), _normalized_version(raw_version)


def _read_pyproject_version(pyproject_path: Path) -> tuple[str, str | None]:
    """Read and validate the pyproject project version."""
    pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    project = pyproject.get("project", {})
    raw_version = project.get("version", "") if isinstance(project, dict) else ""
    return str(raw_version), _normalized_version(raw_version)


def _evaluate_release_gate(
    event_path: Path,
    manifest_path: Path,
    pyproject_path: Path,
) -> ReleaseGateResult:
    """Evaluate release PR labels, branch name, and metadata versions."""
    event = json.loads(event_path.read_text(encoding="utf-8"))
    pull_request = event["pull_request"]
    branch = pull_request["head"]["ref"]
    labels = {label["name"] for label in pull_request.get("labels", [])}

    branch_match = re.fullmatch(rf"release/{VERSION_PATTERN}", branch)
    branch_version = branch_match.group(1) if branch_match else None

    manifest_version, manifest_version_full = _read_manifest_version(manifest_path)
    pyproject_version, pyproject_version_full = _read_pyproject_version(pyproject_path)

    version_matches = (
        branch_version is not None
        and manifest_version_full is not None
        and pyproject_version_full is not None
        and branch_version == manifest_version_full == pyproject_version_full
    )
    should_publish = "release" in labels and version_matches

    if should_publish:
        message = f"Release publishing enabled for branch {branch} with version {manifest_version}"
        return ReleaseGateResult(
            should_publish=True,
            message=message,
            version=manifest_version,
            tag=manifest_version,
            prerelease="rc" in manifest_version,
        )

    message = (
        "Skipping publish because PR must have label 'release', "
        "branch release/X.Y.Z or release/X.Y.Z-rc.N, and matching "
        "manifest.json and pyproject.toml versions; got branch "
        f"{branch!r}, branch version {branch_version!r}, manifest "
        f"version {manifest_version!r}, and pyproject version "
        f"{pyproject_version!r}"
    )
    return ReleaseGateResult(should_publish=False, message=message)


def _write_github_outputs(output_path: Path, result: ReleaseGateResult) -> None:
    """Append release gate outputs for downstream GitHub Actions steps."""
    lines = [f"should_publish={str(result.should_publish).lower()}"]
    if result.should_publish:
        lines.extend(
            [
                f"version={result.version}",
                f"tag={result.tag}",
                f"prerelease={str(result.prerelease).lower()}",
            ]
        )
    with output_path.open("a", encoding="utf-8") as output:
        output.write("\n".join(lines) + "\n")


def _safe_resolve_env_path(env_var: str) -> Path:
    """Resolve and validate a path from an environment variable to prevent path injection.

    Mitigates CWE-22 (Path Traversal) and CWE-73 (Uncontrolled Data Used in Path
    Expression) by gating the user-provided value with sanitizer patterns that
    CodeQL's ``py/path-injection`` query recognizes as barriers:

    - For ``GITHUB_EVENT_PATH``, the value is compared for strict equality
      against a string literal and the returned :class:`~pathlib.Path` is
      constructed from that literal, so the tainted environment value never
      flows into a path expression.
    - For ``GITHUB_OUTPUT``, the value is gated by an inline ``or``-chain of
      :py:meth:`str.startswith` calls against string literals before being used
      in any path expression. After resolution, the same literal-prefix chain
      is reapplied to the canonicalized path as defense-in-depth against
      symlink escape.
    """
    raw_path = os.environ.get(env_var)
    if raw_path is None:
        raise ValueError(f"Environment variable {env_var!r} is not set")

    stripped = raw_path.strip()
    if not stripped:
        raise ValueError(f"Environment variable {env_var!r} is empty")
    if "\x00" in stripped:
        raise ValueError(f"Path from {env_var!r} contains invalid null bytes")
    if not os.path.isabs(stripped):
        raise ValueError(f"Path from {env_var!r} must be absolute")

    if env_var == "GITHUB_EVENT_PATH":
        if stripped != "/github/workflow/event.json":
            raise PermissionError(
                f"Path from {env_var!r} must equal '/github/workflow/event.json': {stripped!r}"
            )
        try:
            return Path("/github/workflow/event.json").resolve(strict=True)
        except OSError as exc:
            raise ValueError("Expected GitHub event file is missing or not resolvable") from exc

    if not stripped.startswith(
        (
            "/home/runner/",
            "/Users/runner/",
            "/github/",
            "/private/tmp/",
            "/var/tmp/",
            "/tmp/",
        )
    ):
        raise PermissionError(
            f"Security Exception: Path from environment variable {env_var!r} is "
            f"outside permitted secure directories: {stripped!r}"
        )

    if ".." in stripped.split("/"):
        raise ValueError(f"Path from {env_var!r} must not contain traversal segments")
    if os.path.normpath(stripped) != stripped:
        raise ValueError(f"Path from {env_var!r} must be normalized without redundant segments")

    try:
        candidate = Path(stripped).resolve(strict=True)
    except OSError as exc:
        raise ValueError(
            f"Path from {env_var!r} must exist and be resolvable: {stripped!r}"
        ) from exc

    resolved = str(candidate)
    if not resolved.startswith(
        (
            "/home/runner/",
            "/Users/runner/",
            "/github/",
            "/private/tmp/",
            "/var/tmp/",
            "/tmp/",
        )
    ):
        raise PermissionError(
            f"Security Exception: Resolved path {resolved!r} from environment "
            f"variable {env_var!r} escapes permitted secure directories"
        )

    if not candidate.is_file():
        raise ValueError(f"Path from {env_var!r} must point to a regular file: {resolved!r}")

    return candidate


def main() -> None:
    """Evaluate release gate inputs from the GitHub Actions environment."""
    event_path = _safe_resolve_env_path("GITHUB_EVENT_PATH")
    output_path = _safe_resolve_env_path("GITHUB_OUTPUT")
    result = _evaluate_release_gate(
        event_path=event_path,
        manifest_path=Path("custom_components/blueprints_updater/manifest.json"),
        pyproject_path=Path("pyproject.toml"),
    )
    print(result.message)
    _write_github_outputs(output_path, result)


if __name__ == "__main__":
    main()
