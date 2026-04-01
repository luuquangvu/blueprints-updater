# Blueprints Updater for Home Assistant

**[ 🇺🇸 English | [🇻🇳 Tiếng Việt](README_vi.md) ]**

**Blueprints Updater** keeps your Home Assistant setup up-to-date by automatically tracking and updating your installed blueprints (Automations, Scripts, and Template Entities). It integrates seamlessly as a native update entity, giving you peace of mind with one-click updates directly from your dashboard.

---

## Features

- **Feels like a native feature**: Integrates seamlessly with Home Assistant, working just like official or HACS updates. You'll get standard update entities on your dashboard, making it easy to stay up-to-date with a single click or bulk update all blueprints at once.
- **Safety first with automatic backups**: Every update is validated for YAML syntax and version compatibility before being applied. The integration also maintains rotating backups, and the update process is designed to be highly reliable. Your files won't be corrupted even in the event of a power outage or system crash.
- **Set it and forget it**: Enable auto-updates and let the system do the work for you. It automatically backs up your current blueprint, downloads the new version, and notifies you exactly which blueprints were updated.
- **Efficient change detection**: Instead of constant downloads, it uses SHA256 hashing and ETag headers to pull data only when an actual change is detected. This saves bandwidth and keeps your system performing at its best.
- **Broad source support**: Whether a blueprint is hosted on GitHub, GitHub Gist, or shared on the Home Assistant Community Forum, this updater handles it accurately.
- **Know the impact before you update**: Displays the exact number of Automations or Scripts currently using a blueprint, so you know exactly how the update will affect your setup.
- **Complete control**: Choose to track all your blueprints or use granulated Whitelists and Blacklists to monitor only the ones you want.
- **No-restart reloads**: Relevant domains (automation, script, or template) are automatically reloaded after an update. Your changes take effect immediately without needing to reboot Home Assistant.
- **Preserves source links**: Automatically maintains the source URL metadata in your YAML files, ensuring your blueprints remain trackable and updatable in the long run.
- **Built-in Security Protection**: Includes automatic Path Safety and URL Safety checks to protect your home network from unauthorized access and ensure all blueprint files stay strictly where they belong.
- **Instant results and dynamic discovery**: New blueprints are detected automatically without a restart. Your entire blueprint list appears instantly whenever you open Home Assistant. No more waiting for slow network scans to finish before you can see your data. The interface is available in several languages and adapts to your Home Assistant settings.

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
6.  Once added, the integration will scan your blueprints. If updates are found, they will appear as `update` entities in your dashboard.

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

## Contributing

Contributions are welcome! If you find a bug or have a feature request, please open an issue or submit a pull request.

## License

This project is licensed under the MIT License.
