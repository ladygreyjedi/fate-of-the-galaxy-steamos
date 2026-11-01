# Fate of the Galaxy on SteamOS

An unofficial installer for the native Linux version of [Fate of the Galaxy](https://fate-of-the-galaxy.github.io/) on x86_64 SteamOS machines and Steam Deck.

One command prepares the launcher, downloads the game and its engine, verifies the files, and asks Steam to add a **Fate of the Galaxy** library entry.

## Install

1. Switch to **Desktop Mode** on your SteamOS device.
2. Download **fate-of-the-galaxy-steamos.zip** from the [latest release](https://github.com/ladygreyjedi/fate-of-the-galaxy-steamos/releases/latest) and extract it into a folder in Downloads.
3. Open Konsole in the extracted folder and run:

   ```bash
   bash install-fate-of-the-galaxy.sh --with-content --no-launch
   ```

   Keep the terminal open until the installation finishes. Allow about **2.5 GiB of downloads** and **13 GiB of free space** during installation.

4. Return to Gaming Mode and open **Library → Non-Steam → Fate of the Galaxy**.

Run as your normal user. No administrator password is needed.

## Compatibility

- **Tested:** SteamOS **3.9.5**, x86_64; Fate of the Galaxy **1.0.2**; FSO **26.0.1**.
- **Required:** Python **3.12+**, `curl`, `7z`, and a running Steam desktop session. These tools were present on the tested SteamOS installation. The installer does not install system packages.
- **Installation location:** `~/Games/Fate-of-the-Galaxy`. Custom locations must be inside your home folder.
- **Native Linux:** Leave Steam's **Force the use of a specific Steam Play compatibility tool** unchecked.
- Installation, file integrity, Steam registration, and native engine startup were checked on one SteamOS machine. Gameplay, controller layouts, other distributions, and other SteamOS versions have not been validated.

The game may need a pilot profile and your preferred controls configured on first use.

## What it does

- Downloads the official self-contained **Knossos.NET 1.3.13** launcher and checks its pinned size and SHA-256.
- Fetches the official Nebula metadata for the pinned game and engine releases. Installation metadata is checked against pinned SHA-256 values before use.
- Verifies all **12 archives** and **12,165 installed files** before publishing the new game library. Download archives are removed after success.
- Starts the installed engine directly. In testing, Knossos 1.3.13's quick-launch command exited before starting the game.
- Uses AppImage's built-in extraction mode, which worked with Steam's launch environment.
- Stores launcher settings and pilot data inside the installation's `launcher/kn_portable/` directory.
- Creates a desktop application entry and requests a non-Steam shortcut through the running Steam client.

The installer keeps generated-file backups when replacing different versions, preserves existing saves, and refuses unrelated nonempty installation folders. It does not change the SteamOS system image, close Steam, or edit Steam's configuration files.

## Updates and options

Open the desktop application entry's **Manage or update game** action, or run:

```bash
python3 "$HOME/Games/Fate-of-the-Galaxy/launch-fate-of-the-galaxy.py" --launcher
```

Knossos handles later game updates. This installer's automatic content download remains pinned to the release versions listed above. It stops if the pinned installation metadata changes.

Other setup options:

```bash
# Install the full game into another folder inside your home directory:
bash install-fate-of-the-galaxy.sh --with-content --no-launch \
  --install-dir "$HOME/Games/FotG"

# Install just the launcher, then use its normal game-download interface:
bash install-fate-of-the-galaxy.sh

# Retry the Steam shortcut request after an existing installation:
bash install-fate-of-the-galaxy.sh --add-to-steam --no-launch
```

If a download fails, rerun the same command. Valid downloaded archives are reused. An existing completed game is preserved; use Knossos to verify or repair it.

## Troubleshooting

**The Steam entry is missing:** Keep Steam running and signed into the intended account. In desktop Steam, use **Games → Add a Non-Steam Game**, select **Fate of the Galaxy**, and add it. The installer can verify that it sent a request, but cannot guarantee the Steam client accepts it on every machine.

**The game does not start:** Check `~/Games/Fate-of-the-Galaxy/launcher-output.log`. Content-download progress and errors are in `.content-install/` inside the installation folder. Confirm that Steam Play compatibility is disabled for the shortcut.

**The catalog checksum changed:** Check for a newer installer release or use Knossos's normal installation interface. The pinned metadata must be reviewed before changing the checksum.

**Another dependency or release is installed through Knossos:** The direct launcher resolves an installed stable game with its exact FSO dependency. If the installed metadata is unsupported or incomplete, it opens Knossos instead.

## Development

The installer uses Bash and Python's standard library, plus the system `curl` and `7z` tools. Run the isolated checks without downloading the real game:

```bash
bash -n install-fate-of-the-galaxy.sh
python3 -m unittest discover -p 'test_*.py' -v
```

## Sources and licensing

- [Official game and Linux installation instructions](https://www.moddb.com/games/fate-of-the-galaxy/downloads/fate-of-the-galaxy-release-linux)
- [Knossos.NET 1.3.13](https://github.com/KnossosNET/Knossos.NET/releases/tag/v1.3.13)
- [Official Nebula catalog](https://fsnebula.org/storage/repo_minimal.json)
- [FSO 26.0.1](https://github.com/scp-fs2open/fs2open.github.com/releases/tag/release_26_0_1)
- [SteamOS desktop instructions](https://help.steampowered.com/en/faqs/view/671A-4453-E8D2-323C/)
- [Steam shortcut registration reference](https://github.com/GloriousEggroll/steam-add-nonsteam-game)

The installer code is provided under the [MIT license](LICENSE). Downloaded software and game assets retain their upstream licenses; no game assets are included in this repository or its release ZIP. This community installer is not affiliated with the Fate of the Galaxy team, Valve, Lucasfilm, or Disney.
