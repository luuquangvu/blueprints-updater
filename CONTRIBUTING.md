# Contributing to Blueprints Updater

Thanks for contributing to Blueprints Updater, a Home Assistant custom integration. Please search existing [issues](https://github.com/luuquangvu/blueprints-updater/issues) before starting substantial work.

## Development setup

Use a POSIX environment; Linux, or WSL are recommended. The project requires Python 3.14.2 or newer, [uv](https://docs.astral.sh/uv/), and Node.js/npm.

```bash
git clone https://github.com/luuquangvu/blueprints-updater.git
cd blueprints-updater
uv sync --all-groups
npm ci
```

Use `uv run` for Python commands. Keep `uv.lock` synchronized with `pyproject.toml`, and keep `package-lock.json` synchronized with `package.json`.

Manage Python dependencies exclusively with uv; do not edit `uv.lock` manually.

## Making changes

- Start from an up-to-date `main` branch and keep each pull request focused.
- Read the affected code and tests before editing.
- Add regression tests for behavior changes.
- Update services, translations, documentation, and metadata when applicable.
- Do not change the integration version in ordinary pull requests.
- Review the complete diff before requesting review.

## Tests and validation

Run a focused test while developing, for example:

```bash
uv run pytest tests/coordinator/test_compatibility_guard.py
```

Run the full local gate before submitting:

```bash
uv run tools/validate.py
```

Validation checks dependency alignment, Ruff, Ty, Pyright, Interrogate, Prettier, and the full pytest suite. It passes only when the output contains `VALIDATION_SUCCESS`; Ruff and Prettier may modify files, so review the diff afterward.

Run the compatibility matrix when changing Home Assistant API usage, compatibility code, dependencies, or `tools/compatibility_matrix.json`:

```bash
uv run tools/validate_compatibility.py
```

For user-facing changes, keep `strings.json` and the translation files aligned. Translation tests enforce their structure and key order.

## AI-assisted contributions

AI coding agents may be used. The author remains primarily responsible for every submitted change, including code produced with AI assistance. The author must understand the change, verify it against the existing code and tests, review the full diff, check for security and compatibility issues, and run the required validation. Do not submit generated code that has not been reviewed and tested.

## Pull requests

Describe what changed, why it changed, affected Home Assistant versions or blueprint sources, and how it was tested. Include relevant security, persistence, translation, or compatibility considerations.

CI also runs Home Assistant Hassfest, HACS validation, formatting, static checks, tests, and compatibility checks as applicable.

## Reporting issues

Use the [bug report](https://github.com/luuquangvu/blueprints-updater/issues/new?template=bug_report.yaml), [feature request](https://github.com/luuquangvu/blueprints-updater/issues/new?template=feature_request.yaml), or [other issue](https://github.com/luuquangvu/blueprints-updater/issues/new?template=other.yaml) template. Include your Home Assistant and integration versions, language, exact input, expected result, actual result, and any relevant diagnostics. Be sure to remove any private household details before posting.

## License

By contributing, you agree that your contributions are distributed under the project's [MIT License](LICENSE).
