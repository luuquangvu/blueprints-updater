# Blueprints Updater for Home Assistant

**[ 🇺🇸 English | [🇻🇳 Tiếng Việt](README_vi.md) ]**

**Blueprints Updater** is a Home Assistant integration that allows you to automatically track and update your installed blueprints (**Automations**, **Scripts**, and **Template Entities**) from their original source URLs. It works just like a native `update` entity, providing notifications and a one-click update button when a new version of a blueprint is detected.

---

## Features

- **Automated Periodic Scanning**: Scans your local `blueprints/` directory every X hours (configurable).
- **Smart Comparison**: Uses modern **SHA256** hashing to compare your local file content with the latest version from the source URL.
- **Advanced URL Support**:
  - **GitHub**: Automatically converts standard blob URLs to raw file URLs.
  - **Gist**: Supports GitHub Gists natively.
  - **HA Community Forum**: Directly parses forum topics to extract the latest YAML blueprint code.
- **Source Persistence**: Automatically ensures the `source_url` tag is preserved in updated files so future updates always work.
- **Advanced Filtering**: Choose to update all blueprints, only specific ones (**Whitelist**), or exclude specific ones (**Blacklist**).
- **Auto-Update Support**: Optional feature to automatically download and apply updates as soon as they are detected.
- **Improved Reload Logic**: Automatically reloads `automation`, `script`, and `template` domains after an update to ensure immediate effect.
- **Safety First**: Validates remote YAML content before updates. If the remote file has syntax errors, the update is blocked to protect your local configuration, and informative error messages are provided.
- **Usage Insight**: Before updating, the integration calculates and displays the exact number of Automations and Scripts that currently rely on the blueprint, helping you understand the impact of the update.
- **Backup & Restore**: Automatically create backups before updating blueprints. If an update breaks your automations, easily restore the previous version with a single service call. (Auto-updates are always backed up).
- **Update All (Bulk Update)**: Instantly update all eligible blueprints simultaneously via the `blueprints_updater.update_all` service, without freezing Home Assistant.
- **Manual Refresh**: Trigger an immediate scan via the **`blueprints_updater.reload`** action in Developer Tools.
- **Dynamic Discovery**: Automatically detects and adds new blueprints as `update` entities without requiring a restart.

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
3.  **Enable Auto-Update**: (Optional) If enabled, blueprints matching your criteria will be updated automatically without manual intervention.
4.  Choose your **Update Interval** (default is 24 hours).
5.  Select your **Filter Mode**:
    - **Update All**: Tracks all blueprints found in your directory.
    - **Whitelist**: Only tracks blueprints you explicitly select from the list.
    - **Blacklist**: Tracks all blueprints _except_ the ones you select.
6.  Once added, the integration will scan your blueprints. If updates are found, they will appear as `update` entities in your dashboard.

### Backup & Restore

Blueprints Updater provides a built-in safety net by allowing you to back up blueprints before they are updated and restore them if needed.

#### Enabling Backups

When installing an update from the Home Assistant dashboard, you will have the option to check the **Backup** toggle. If enabled, the integration will automatically save your current blueprint to a `.bak` file before replacing it with the new version.

> **Note:** If you have **Auto-Update** enabled in the integration settings, it will **always** create a backup automatically before applying an update, providing a guaranteed safety net with zero effort required.

#### Restoring a Backup

If you find that a newly updated blueprint breaks your automations or has an incompatible change, you can easily revert to the previous version:

1. Go to **Developer Tools** > **Actions** (or **Services**).
2. Search for the **`blueprints_updater.restore_blueprint`** action.
3. Select the `update` entity associated with the blueprint you want to restore.
4. Click **Perform Action**.

The integration will look for the `.bak` file, restore the original YAML content, and automatically reload your automations and scripts to apply the change immediately.

### Applying Changes (Adding/Deleting Blueprints)

Since Home Assistant does not constantly monitor the file system to save resources, **newly added or deleted blueprints will not be reflected immediately**.

To apply changes instantly without waiting for the next scheduled background scan, you must do **one** of the following:

1. **Run the Reload Action (Recommended)**: Go to **Developer Tools** > **Actions**, search for **`blueprints_updater.reload`**, and click **Perform Action**. The integration will instantly scan your blueprints directory and create any new entities or update statuses.
2. **Reload the Integration**: Go to **Settings** > **Devices & Services** > **Blueprints Updater**, click the three dots, and select **Reload**.
3. **Restart Home Assistant**.

### Requirements

For a blueprint to be trackable, it **must** contain a valid `source_url` within its metadata:

```yaml
blueprint:
  name: "My Blueprint"
  source_url: https://github.com/user/repo/blob/main/blueprint.yaml
  # ...
```

---

## Contributing

Contributions are welcome! If you find a bug or have a feature request, please open an issue or submit a pull request.

## License

This project is licensed under the MIT License.
