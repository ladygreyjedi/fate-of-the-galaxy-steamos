#!/usr/bin/env bash
# Fate of the Galaxy setup for an x86_64 SteamOS machine.
# Run from Desktop Mode: bash install-fate-of-the-galaxy.sh
# Only Python's standard library is needed. Do not run with sudo.
set -euo pipefail
FOTG_INSTALLER_DIRECTORY="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
export FOTG_INSTALLER_DIRECTORY
if ! command -v python3 >/dev/null 2>&1; then
    printf '%s\n' 'Python 3 is required. This script is intended for stock SteamOS.' >&2
    exit 1
fi
exec python3 - "$@" <<'FOTG_INSTALLER_PY'
import argparse
import hashlib
import http.client
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Verified against the official GitHub release and Nebula catalog on 2026-10-31.
# Pin the launcher for a repeatable, checksum-verified installation. Knossos can
# subsequently update itself and the game through its normal interface.
VERSION = "v1.3.13"
DOWNLOAD_URL = (
    "https://github.com/KnossosNET/Knossos.NET/releases/download/"
    + VERSION + "/Linux_x64.tar.gz"
)
ARCHIVE_SHA256 = "5301ddfca870b6f0505e464b578e1ea4cdd45fc6395653bd185711361217bdc6"
ARCHIVE_SIZE = 45364070
MOD_ID = "FotG_R"
OWNER = "fotg-steamos-installer-v1"
MARKER = ".fotg-steamos-install.json"
DESKTOP_ID = "io.github.fotg.SteamOS.desktop"

LAUNCHER_CODE = r'''#!/usr/bin/env python3
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

MOD_ID = "FotG_R"

def direct_game(library):
    candidates = []
    for manifest in (library / MOD_ID).glob("*/mod.json"):
        if (manifest.parent / "knossos_net_download.token").exists():
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8-sig"))
            version = data.get("version", "")
            if data.get("id") == MOD_ID and data.get("installed") is True and re.fullmatch(r"\d+\.\d+\.\d+", version):
                candidates.append((tuple(map(int, version.split("."))), manifest, data))
        except (OSError, ValueError, AttributeError, TypeError):
            continue
    for _, manifest, data in sorted(candidates, reverse=True):
        try:
            dependencies = {(d["id"], d.get("version"))
                            for p in data["packages"] for d in p.get("dependencies", [])}
            if len(dependencies) != 1 or data.get("mod_flag") != [MOD_ID]:
                continue
            engine_id, engine_version = next(iter(dependencies))
            if engine_id != "FSO" or not re.fullmatch(r"\d+\.\d+\.\d+", engine_version or ""):
                continue
            engine_root = library / "bin" / (engine_id + "-" + engine_version)
            if (engine_root / "knossos_net_download.token").exists():
                continue
            engine = json.loads((engine_root / "mod.json").read_text(encoding="utf-8-sig"))
            if engine.get("installed") is not True or engine.get("id") != engine_id or engine.get("version") != engine_version:
                continue
            for package in engine["packages"]:
                if package.get("environment") != "linux && x86_64":
                    continue
                for executable in package.get("executables", []):
                    if executable.get("label") is not None:
                        continue
                    binary = (engine_root / executable["file"]).resolve()
                    if not binary.is_relative_to(engine_root.resolve()) or not binary.is_file() or not os.access(binary, os.X_OK):
                        continue
                    command = [str(binary), "-parse_cmdline_only"]
                    command += shlex.split(data.get("cmdline") or "")
                    command += ["-mod", manifest.parent.name]
                    return command, manifest.parent.parent
        except (OSError, ValueError, AttributeError, KeyError, TypeError):
            continue
    return None

def main():
    root = Path(__file__).resolve().parent
    binary = root / "launcher" / "Knossos.NET"
    library = root / "launcher" / "kn_portable" / "Library"
    command = [str(binary)]
    working_directory = binary.parent
    environment = os.environ.copy()
    environment["DOTNET_BUNDLE_EXTRACT_BASE_DIR"] = str(root / "runtime-cache")
    # Steam's runtime may not expose fusermount; use AppImage's supported mode.
    environment["APPIMAGE_EXTRACT_AND_RUN"] = "1"
    direct = direct_game(library) if "--launcher" not in sys.argv[1:] else None
    if direct:
        command, working_directory = direct
        preferences = root / "launcher" / "kn_portable" / "HardLightProductions" / MOD_ID
        preferences.mkdir(parents=True, exist_ok=True)
        environment["FSO_PREFERENCES_PATH"] = str(preferences) + os.sep
    log_path = root / "launcher-output.log"
    with log_path.open("a", encoding="utf-8") as log:
        try:
            print("Starting: " + repr(command), file=log, flush=True)
            result = subprocess.run(command, cwd=str(working_directory), env=environment,
                                    stdout=log, stderr=subprocess.STDOUT)
            code = result.returncode
            print("Process exit: " + str(code), file=log, flush=True)
        except OSError as error:
            print(str(error), file=log)
            code = 1
    if code:
        message = "Fate of the Galaxy could not start. Details are in:\n" + str(log_path)
        dialog = shutil.which("kdialog")
        if dialog:
            subprocess.run([dialog, "--title", "Fate of the Galaxy", "--error", message],
                           check=False)
        else:
            print(message, file=sys.stderr)
    return code if 0 <= code <= 255 else 1

if __name__ == "__main__":
    sys.exit(main())
'''


class SetupError(Exception):
    pass


def preflight():
    if sys.platform != "linux" or platform.machine().lower() not in ("x86_64", "amd64"):
        raise SetupError("Run this on your x86_64 SteamOS machine, not on the Mac.")
    if os.geteuid() == 0:
        raise SetupError("Run as your normal SteamOS user, without sudo.")
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise SetupError("Switch to Desktop Mode and run this in Konsole.")


def write_text(path, content, mode=0o644):
    """Replace our generated files atomically; preserve any differing old copy."""
    if path.is_symlink():
        raise SetupError("Refusing to replace a symbolic link: " + str(path))
    encoded = content.encode("utf-8")
    if path.exists() and path.read_bytes() == encoded:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_name(path.name + ".bak-" + str(time.time_ns()))
        shutil.copy2(path, backup)
    descriptor, temporary = tempfile.mkstemp(prefix=".fotg-", dir=str(path.parent))
    temporary = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(encoded)
        temporary.chmod(mode)
        os.replace(str(temporary), str(path))
    finally:
        if temporary.exists():
            temporary.unlink()


def save_state(root, state):
    # This receipt contains only this installer's state, not Knossos preferences.
    write_text(root / MARKER, json.dumps(state, indent=2) + "\n", 0o600)


def read_state(root):
    marker = root / MARKER
    if not root.exists():
        return None
    if marker.is_symlink():
        raise SetupError("The installation receipt is a symbolic link: " + str(marker))
    if not marker.exists():
        if any(root.iterdir()):
            raise SetupError("This folder already contains other files: " + str(root)
                             + "\nUse --install-dir with a new folder inside your home folder.")
        return None
    try:
        state = json.loads(marker.read_text(encoding="utf-8"))
        if state.get("owner") != OWNER or state.get("mod_id") != MOD_ID:
            raise ValueError("unrecognized receipt")
    except (ValueError, AttributeError) as error:
        raise SetupError("Unrecognized installation receipt: " + str(marker)) from error
    return state


def download_archive(destination):
    print("Downloading the official Knossos.NET " + VERSION + " Linux launcher (43 MiB)...",
          flush=True)
    for attempt in range(1, 4):
        try:
            request = urllib.request.Request(DOWNLOAD_URL,
                                             headers={"User-Agent": "FotG-SteamOS-Setup/1.0"})
            digest = hashlib.sha256()
            downloaded = 0
            last_report = 0
            with urllib.request.urlopen(request, timeout=45) as response, destination.open("wb") as output:
                if not response.geturl().startswith("https://"):
                    raise SetupError("The download was redirected away from HTTPS.")
                while True:
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    downloaded += len(block)
                    if downloaded > ARCHIVE_SIZE:
                        raise SetupError("The download is larger than the verified release archive.")
                    digest.update(block)
                    output.write(block)
                    if downloaded - last_report >= 8 * 1024 * 1024:
                        print("  Downloaded " + str(downloaded // (1024 * 1024)) + " MiB", flush=True)
                        last_report = downloaded
            if digest.hexdigest() != ARCHIVE_SHA256 or downloaded != ARCHIVE_SIZE:
                raise SetupError("Download verification failed. No launcher was installed. Run again to retry.")
            print("SHA-256 verified.", flush=True)
            return
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException) as error:
            if attempt == 3:
                raise SetupError("Could not download the launcher: " + str(error)) from error
            print("Download interrupted; retrying (" + str(attempt + 1) + "/3)...", flush=True)


def unpack_launcher(archive, directory):
    """Extract only the expected binary; never extract archive paths or links."""
    directory.mkdir(parents=True, exist_ok=True)
    with tarfile.open(str(archive), "r:gz") as source:
        members = source.getmembers()
        if len(members) != 1 or members[0].name != "Knossos.NET" or not members[0].isfile():
            raise SetupError("Unexpected launcher archive contents; nothing was extracted.")
        if not 0 < members[0].size < 256 * 1024 * 1024:
            raise SetupError("Unexpected launcher binary size.")
        incoming = source.extractfile(members[0])
        if incoming is None:
            raise SetupError("The launcher binary could not be read.")
        with incoming, (directory / "Knossos.NET").open("wb") as output:
            shutil.copyfileobj(incoming, output)
    (directory / "Knossos.NET").chmod(0o755)


def desktop_string(value):
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")


def desktop_argument(value):
    value = str(value)
    for character in ("\\", '"', "`", "$"):
        value = value.replace(character, "\\" + character)
    # Desktop Entry string escapes are processed before Exec argument quoting.
    return desktop_string('"' + value + '"').replace("%", "%%")


def desktop_content(root):
    command = desktop_argument(sys.executable) + " " + desktop_argument(root / "launch-fate-of-the-galaxy.py")
    return (
        "[Desktop Entry]\nType=Application\nVersion=1.0\nName=Fate of the Galaxy\n"
        "Comment=Play Fate of the Galaxy, or finish its installation\n"
        "Exec=" + command + "\nPath=" + desktop_string(root) + "\n"
        "Icon=applications-games\nTerminal=false\nStartupNotify=false\nCategories=Game;\n"
        "Actions=Manage;\nX-FotG-Installer=1\n\n[Desktop Action Manage]\n"
        "Name=Manage or update game\nExec=" + command + " --launcher\n"
    )


def install(root, applications):
    state = read_state(root)
    desktop = applications / DESKTOP_ID
    if desktop.is_symlink() or (desktop.exists() and "X-FotG-Installer=1\n" not in desktop.read_text(encoding="utf-8")):
        raise SetupError("An unrelated application entry already uses: " + str(desktop))
    root.parent.mkdir(parents=True, exist_ok=True)
    if state is None:
        if shutil.disk_usage(str(root.parent)).free < 512 * 1024 * 1024:
            raise SetupError("At least 512 MiB of free space is needed for launcher setup."
                             " Knossos will check the additional game download space.")
        with tempfile.TemporaryDirectory(prefix=".fotg-setup-", dir=str(root.parent)) as temporary:
            stage = Path(temporary)
            archive = stage / "launcher.tar.gz"
            download_archive(archive)
            staged_root = stage / "installation"
            unpack_launcher(archive, staged_root / "launcher")
            portable = staged_root / "launcher" / "kn_portable"
            (portable / "Library").mkdir(parents=True)
            config = {
                "ModID": MOD_ID,
                "WindowTitle": "Fate of the Galaxy",
                "UseCustomFSODataFolder": True,
                "AllowLauncherUpdates": True,
                "MenuDisplayGlobalSettingsEntry": True,
                "MenuOpenFirstTime": True,
            }
            write_text(portable / "KnossosNET" / "custom_launcher.json",
                       json.dumps(config, indent=2) + "\n")
            state = {"owner": OWNER, "mod_id": MOD_ID, "initial_launcher": VERSION,
                     "steam_registration_requested": False}
            save_state(staged_root, state)
            if root.exists() and any(root.iterdir()):
                raise SetupError("The destination changed during the download. Rerun setup.")
            os.replace(str(staged_root), str(root))
    else:
        binary = root / "launcher" / "Knossos.NET"
        if not binary.is_file() or not os.access(str(binary), os.X_OK):
            raise SetupError("The existing launcher is missing or not executable: " + str(binary)
                             + "\nYour library was preserved. Use a new --install-dir for a separate setup.")
        print("Reusing the existing launcher and game library; settings and saves are preserved.")
    write_text(root / "launch-fate-of-the-galaxy.py", LAUNCHER_CODE, 0o755)
    write_text(desktop, desktop_content(root), 0o755)
    return state, desktop


def request_steam_shortcut(desktop):
    steam = shutil.which("steam")
    if steam is None:
        print("Steam's command was not found. Add 'Fate of the Galaxy' from Steam's Add a Non-Steam Game menu.")
        return False
    # This is the same local-file gate used by SteamOS's Add to Steam action.
    sentinel = Path("/tmp/addnonsteamgamefile")
    try:
        descriptor = os.open(str(sentinel), os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        os.close(descriptor)
        uri = "steam://addnonsteamgame/" + urllib.parse.quote(str(desktop), safe="")
        subprocess.Popen([steam, uri], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError as error:
        print("Steam shortcut request could not be sent: " + str(error))
        return False
    print("Requested the Steam shortcut. Check Library > Non-Steam for 'Fate of the Galaxy'.")
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(prog="install-fate-of-the-galaxy.sh",
                                     description="Set up Fate of the Galaxy's native Linux launcher on SteamOS.")
    parser.add_argument("--install-dir", type=Path, default=Path.home() / "Games" / "Fate-of-the-Galaxy")
    parser.add_argument("--no-launch", action="store_true", help="Prepare the launcher without opening it")
    parser.add_argument("--with-content", action="store_true", help="Download and verify the pinned game release and Linux engine")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--no-steam", action="store_true", help="Do not request a Steam shortcut")
    group.add_argument("--add-to-steam", action="store_true", help="Retry the Steam shortcut request on an existing setup")
    args = parser.parse_args(argv)
    preflight()
    if args.with_content and sys.version_info < (3, 12):
        raise SetupError("Python 3.12 or newer is required for automatic content installation.")
    root = args.install_dir.expanduser().resolve()
    home = Path.home().resolve()
    if root == home or home not in root.parents or any(ord(c) < 32 for c in str(root)):
        raise SetupError("Choose a normal folder inside your home folder for --install-dir.")
    applications = Path(os.environ.get("XDG_DATA_HOME", str(home / ".local" / "share"))) / "applications"
    state, desktop = install(root, applications)
    if args.with_content:
        # This Python program is embedded in a shell here-document, so __file__ is '-'.
        content_script = Path(os.environ.get("FOTG_INSTALLER_DIRECTORY", str(Path.cwd()))) / "install-game-content.py"
        if not content_script.is_file():
            raise SetupError("Keep install-game-content.py alongside this setup script. Download the complete release ZIP.")
        result = subprocess.run([sys.executable, str(content_script), "--install-dir", str(root)])
        if result.returncode:
            raise SetupError("Game installation stopped. The launcher and existing saves were preserved; rerun to retry.")
    if not args.no_steam:
        if args.add_to_steam or not state.get("steam_registration_requested"):
            state["steam_registration_requested"] = request_steam_shortcut(desktop)
            save_state(root, state)
        else:
            print("A Steam shortcut was already requested. Use --add-to-steam if it is missing.")
    if not args.no_launch:
        launch_command = [sys.executable, str(root / "launch-fate-of-the-galaxy.py")]
        if not args.with_content:
            launch_command.append("--launcher")
        subprocess.Popen(launch_command,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    if args.with_content:
        print("\nFate of the Galaxy and its Linux engine are installed and checksum-verified.")
    else:
        print("\nLauncher setup complete. The game content still needs to download.")
        print("In the Fate of the Galaxy window: click Install, then OK. Wait for all downloads to finish.")
    print("Then return to Gaming Mode and launch Fate of the Galaxy from Library > Non-Steam.")
    print("The Steam shortcut opens the game directly once its installation is complete.")
    print("If the shortcut is missing: desktop Steam > Games > Add a Non-Steam Game > Fate of the Galaxy.")
    print("For updates: right-click Fate of the Galaxy in the desktop application menu > Manage or update game.")
    print("Installed in: " + str(root))
    print("Startup log: " + str(root / "launcher-output.log"))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (SetupError, OSError, UnicodeError, tarfile.TarError) as error:
        print("\nSetup stopped: " + str(error), file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nSetup cancelled. Existing game files were preserved.", file=sys.stderr)
        sys.exit(130)
FOTG_INSTALLER_PY
