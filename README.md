# Blueprints Updater for Home Assistant

[![Release](https://img.shields.io/github/v/release/luuquangvu/blueprints-updater?style=flat&logo=github&label=Release&color=purple)](https://github.com/luuquangvu/blueprints-updater/releases)
[![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg?style=flat&logo=homeassistantcommunitystore&label=HACS)](https://github.com/hacs/integration)
[![Home Assistant](https://img.shields.io/badge/homeassistant-%3E%3D%202024.12.0-03a9f4?style=flat&logo=homeassistant&label=Home+Assistant)](https://www.home-assistant.io)

[![CI](https://img.shields.io/github/actions/workflow/status/luuquangvu/blueprints-updater/ci.yaml?style=flat&logo=github&label=CI)](https://github.com/luuquangvu/blueprints-updater/actions/workflows/ci.yaml)
[![Validation](https://img.shields.io/github/actions/workflow/status/luuquangvu/blueprints-updater/validation.yaml?style=flat&logo=github&label=Validation)](https://github.com/luuquangvu/blueprints-updater/actions/workflows/validation.yaml)
[![CodeQL](https://img.shields.io/github/actions/workflow/status/luuquangvu/blueprints-updater/github-code-scanning/codeql?style=flat&logo=github&label=CodeQL)](https://github.com/luuquangvu/blueprints-updater/actions/workflows/github-code-scanning/codeql)
[![Prettier](https://img.shields.io/github/actions/workflow/status/luuquangvu/blueprints-updater/prettier.yaml?style=flat&logo=prettier&label=Prettier)](https://github.com/luuquangvu/blueprints-updater/actions/workflows/prettier.yaml)

**[ 🇺🇸 English | [🇻🇳 Tiếng Việt](README_vi.md) ]**

**Blueprints Updater** keeps your Home Assistant setup up-to-date by automatically tracking and updating your installed blueprints (Automations, Scripts, and Template Entities). It integrates seamlessly as a native update entity, giving you peace of mind with one-click updates directly from your dashboard.

---

## Features

- **Seamless Native Integration**: Blends perfectly into the Home Assistant ecosystem, looking and feeling like a core feature. Manage everything directly from your dashboard with single-click or bulk updates.
- **Advanced Compatibility Guard**: Protect your smart home from breaking changes with a proactive safeguard. The system performs deep cross-validation of all dependent automations and scripts against the new blueprint content before applying updates, identifying risks specifically as "compatibility errors" to prevent silent failures.
- **Set It and Forget It**: Automate your entire workflow. Enable auto-updates and let the system handle backups, downloads, and change notifications for you.
- **Smart Change Detection**: Minimizes system overhead by using SHA256 hashing and ETag headers to pull data only when a genuine change is detected.
- **Universal Source Compatibility**: Robustly handles blueprints from GitHub, GitHub Gist, and the Home Assistant Community Forum.
- **High-Performance CDN Support**: Leverages the [jsDelivr](https://www.jsdelivr.com/) CDN for GitHub-hosted blueprints to maximize download speeds and reduce the impact of rate limiting. Features an intelligent fallback mechanism that automatically reverts to the original source in case of CDN unavailability.
- **Pre-Update Impact Visibility**: See exactly how many Automations or Scripts use the blueprint before you update, ensuring full control over your smart home logic.
- **Granular Tracking Control**: Fine-tune your experience by tracking all blueprints or targeting specific ones using flexible Whitelists and Blacklists.
- **Instant, Restart-Free Reloads**: Automatically reloads relevant automation, script, or template domains after an update for immediate results without rebooting.
- **Preserves Link Metadata**: Automatically maintains `source_url` metadata in your YAML, ensuring your blueprints remain trackable and updatable for years.
- **Hardened Path & URL Security**: Built-in safety checks protect your local environment from unauthorized access, ensuring all files stay strictly where they belong.
- **Dynamic Discovery**: Automatically detects new blueprints without a restart. Fully localized with multi-language support that adapts to your preferences.

---

## Installation

### Option 1: Using HACS (Recommended)

[![Add Blueprints Updater to HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=luuquangvu&repository=blueprints-updater&category=integration)

1.  Open **HACS** in Home Assistant.
2.  Search for **Blueprints Updater**.
3.  If not found, click the three dots in the top right corner and select **Custom repositories**.
4.  Add `https://github.com/luuquangvu/blueprints-updater` with category **Integration**.
5.  Search for **Blueprints Updater** and click **Download**.
6.  Restart Home Assistant.

### Option 2: Manual Installation

1.  Download the latest release and extract the files.
2.  Copy the `custom_components/blueprints_updater` folder into your Home Assistant `config/custom_components/` directory.
3.  Restart Home Assistant.

---

## Setup & Configuration

1.  Go to **Settings** > **Devices & Services**.
2.  Click **Add Integration** and search for **Blueprints Updater**.
3.  **Enable Auto-Update**: (Optional) If enabled, blueprints matching your criteria will be updated automatically without manual intervention. **A persistent notification will appear** after each successful auto-update to keep you informed of which blueprints were updated.
4.  Choose your **Update Interval** (default is 24 hours).
5.  Select your **Filter Mode**:
    - **Update All**: Tracks all blueprints found in your directory.
    - **Whitelist**: Only tracks blueprints you explicitly select from the list.
    - **Blacklist**: Tracks all blueprints _except_ the ones you select.
6.  **Use jsDelivr CDN**: (Optional, default: Enabled) Optimizes your updates by fetching GitHub-hosted blueprints via the jsDelivr CDN. If the CDN is unreachable or encounters an error, the integration will seamlessly fall back to the original GitHub URL to ensure uninterrupted service.
7.  Once added, the integration will scan your blueprints. If updates are found, they will appear as `update` entities in your dashboard.

### Backup & Restore

Blueprints Updater provides a built-in safety net by allowing you to back up blueprints before they are updated and restore them if needed.

#### Enabling Backups

When installing an update from the Home Assistant dashboard, you will have the option to check the **Backup** toggle. If enabled, the integration will automatically save your current blueprint to a numbered backup file (`.bak.1`, `.bak.2`, etc.) before replacing it with the new version.

> **Note:** If you have **Auto-Update** enabled in the integration settings, it will **always** create a backup automatically before applying an update, providing a guaranteed safety net with zero effort required.

#### Restoring a Backup

If you find that a newly updated blueprint breaks your automations or has an incompatible change, you can easily revert to the previous version:

1. Go to **Developer Tools** > **Actions**. _Note: Administrative privileges are required._
2. Search for the **`blueprints_updater.restore_blueprint`** action.
3. Select the `update` entity associated with the blueprint you want to restore.
4. (Optional) Provide the **Backup Version** you wish to restore (default is **1** for the most recent).
5. Click **Perform Action**.

The integration will look for the specified numbered backup file, restore the original YAML content, and automatically reload your automations and scripts to apply the change immediately.

### Advanced Compatibility Guard

The **Advanced Compatibility Guard** is a professional-grade safety layer designed to protect your smart home logic from breaking changes in blueprint updates.

When an update is detected, the system performs a multi-stage safety check:

1.  **Code Validation**: Automatically verifies the new blueprint's structure to ensure it complies with Home Assistant's rules.
2.  **Impact Analysis**: Simulates the update against your existing automations to see if anything will break.
3.  **Risk Alerts**: If a problem is found (such as a missing required setting), the update is flagged with a **"compatibility error"**.
4.  **Auto-Update Protection**: Blueprints at risk of causing errors are automatically blocked from auto-updating (**"blocked-auto-update"**) to safeguard your smart home from unexpected incidents.
5.  **Full Transparency**: For blocked updates, you can review the **"UPDATE RISK REPORT"** to see specific breaking changes (like missing mandatory inputs) and use the **"Git Diff"** section for a detailed code comparison before proceeding manually.

### Refreshing the Blueprint List

Since Home Assistant does not constantly monitor the file system to save resources, **newly added or deleted blueprints will not be reflected immediately**.

To apply changes instantly without waiting for the next scheduled background scan, you must do **one** of the following:

1. **Run the Reload Action (Recommended)**: Go to **Developer Tools** > **YAML**, find **Blueprints Updater** in the **YAML configuration reloading** list, and click **Reload**. Alternatively, use the **`blueprints_updater.reload`** action in **Developer Tools** > **Actions** (Administrator only).
2. **Reload the Integration**: Go to **Settings** > **Devices & Services** > **Blueprints Updater**, click the three dots, and select **Reload**.
3. **Restart Home Assistant**.

### See it in action!

If you want to see how the update process works immediately, you can use the **Motion-Activated Light/Switch (Frequent Updates)** blueprint. This blueprint is updated automatically frequently via GitHub Actions to simulate a new release.

**Quick Import:**
[![Import Blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Fluuquangvu%2Fdemo-blueprints%2Fblob%2Fmain%2Fblueprints%2Fmotion_light_blueprint.yaml)

**Manual Import:**

1.  Copy this URL: `https://github.com/luuquangvu/demo-blueprints/blob/main/blueprints/motion_light_blueprint.yaml`
2.  In Home Assistant, go to **Settings** > **Automations & Scenes** > **Blueprints**.
3.  Click **Import Blueprint** and paste the URL.

Once imported, **Blueprints Updater** will detect it on the next scheduled scan. To see it immediately, you can [trigger a manual refresh](#refreshing-the-blueprint-list). When the GitHub Action updates the blueprint, you will receive a notification and can perform the update.

---

## Requirements

For a blueprint to be trackable, it **must** contain a valid `source_url` within its metadata:

```yaml
blueprint:
  name: "My Blueprint"
  source_url: https://github.com/user/repo/blob/main/blueprint.yaml
  # ...
```

---

## Code Quality & Security

To ensure long-term reliability and stability, this project utilizes a modern stack of automated development and security tools:

- **Automated Code Review**: [CodeRabbit](https://coderabbit.ai) provides deep analysis of every Pull Request, identifying potential logic flaws and edge cases before they reach your system.
- **Code Optimization**: [Sourcery](https://sourcery.ai) suggests cleaner, more idiomatic Python patterns to maintain a high-quality codebase.
- **Static Analysis & Security**: [CodeQL](https://codeql.github.com) performs industry-standard scans to detect security vulnerabilities and ensure compliance with best practices.
- **Rigorous Development Workflow**:
  - **[Ruff](https://github.com/astral-sh/ruff)**: High-performance linting and formatting for consistent Python code.
  - **[Ty](https://github.com/astral-sh/ty)** & **[Pyright](https://github.com/Microsoft/pyright)**: Type checking to help prevent runtime errors and enhance stability.
  - **[Pytest](https://github.com/pytest-dev/pytest)**: A comprehensive test suite ensuring every update is functional and regression-free.
  - **[Prettier](https://github.com/prettier/prettier)**: Consistent formatting for documentation and configuration files.

> [!NOTE]
> All automated insights are manually reviewed and validated by the project maintainer to ensure every change aligns with the project's standards.

## Contributing

Contributions are what make the open-source community such an amazing place to learn, inspire, and create. Any contributions you make are **greatly appreciated**.

- **If you find a bug**, please help us improve by [opening an issue](https://github.com/luuquangvu/blueprints-updater/issues).
- **If you'd like to contribute**, feel free to fork the repo and create a Pull Request (please ensure your code passes the [quality checks](#code-quality--security) mentioned above).

## License

Distributed under the **MIT License**. See [LICENSE](LICENSE) for more information.
