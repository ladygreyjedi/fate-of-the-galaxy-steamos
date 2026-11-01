"""Isolated installer and launch-flow checks. No real Steam or network access."""
import contextlib
import hashlib
import http.client
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).with_name("install-fate-of-the-galaxy.sh")
source = SCRIPT.read_text(encoding="utf-8").split("<<'FOTG_INSTALLER_PY'\n", 1)[1]
source = source.rsplit("\nFOTG_INSTALLER_PY", 1)[0]
setup = types.ModuleType("fotg_setup")
exec(compile(source, str(SCRIPT), "exec"), setup.__dict__)


FAKE_BINARY = b'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
record = next(p for p in Path(__file__).resolve().parents
              if (p / ".fotg-steamos-install.json").exists()) / "launch-record.json"
record.write_text(json.dumps({"args": sys.argv[1:], "cwd": os.getcwd(),
                             "cache": os.environ.get("DOTNET_BUNDLE_EXTRACT_BASE_DIR"),
                             "preferences": os.environ.get("FSO_PREFERENCES_PATH")}))
'''


def make_archive(name="Knossos.NET", content=FAKE_BINARY, member_type=tarfile.REGTYPE):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        member = tarfile.TarInfo(name)
        member.type = member_type
        if member_type == tarfile.SYMTYPE:
            member.linkname = "../../unrelated-file"
        else:
            member.size = len(content)
        archive.addfile(member, io.BytesIO(content) if member.isfile() else None)
    return output.getvalue()


class Response(io.BytesIO):
    def geturl(self):
        return setup.DOWNLOAD_URL


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="fotg-tests-")
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve() / "A user's Steam home"
        self.home.mkdir()
        self.root = self.home / "Games" / "Fate of the Galaxy"
        self.apps = self.home / ".local" / "share" / "applications"
        self.archive = make_archive()
        self.addCleanup(mock.patch.stopall)
        self.urlopen = mock.patch.object(setup.urllib.request, "urlopen",
                                         side_effect=lambda *a, **k: Response(self.archive)).start()
        mock.patch.object(setup, "ARCHIVE_SHA256", hashlib.sha256(self.archive).hexdigest()).start()
        mock.patch.object(setup, "ARCHIVE_SIZE", len(self.archive)).start()
        self.output = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.output)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)

    def install(self):
        return setup.install(self.root, self.apps)

    def run_helper(self, *arguments):
        result = subprocess.run([sys.executable, str(self.root / "launch-fate-of-the-galaxy.py"), *arguments],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads((self.root / "launch-record.json").read_text())

    def add_game(self):
        folder = self.root / "launcher" / "kn_portable" / "Library" / "FotG_R" / "FotG_R-1.0.2"
        folder.mkdir(parents=True)
        (folder / "mod.json").write_text(json.dumps({"id": "FotG_R", "version": "1.0.2", "installed": True,
            "cmdline": "-aa -soft_particles -deferred_cockpit", "mod_flag": ["FotG_R"],
            "packages": [{"dependencies": [{"id": "FSO", "version": "26.0.1"}]}]}))
        engine = self.root / "launcher/kn_portable/Library/bin/FSO-26.0.1"
        engine.mkdir(parents=True)
        binary = engine / "game.AppImage"
        binary.write_bytes(FAKE_BINARY)
        binary.chmod(0o755)
        (engine / "mod.json").write_text(json.dumps({"id": "FSO", "version": "26.0.1", "installed": True,
            "packages": [{"environment": "linux && x86_64", "executables": [{"file": "game.AppImage", "label": None}]}]}))
        return folder

    def test_first_install_preselects_game_and_opens_setup(self):
        state, desktop = self.install()
        self.assertEqual(state["owner"], setup.OWNER)
        self.assertTrue(desktop.exists())
        portable = self.root / "launcher" / "kn_portable"
        config = json.loads((portable / "KnossosNET" / "custom_launcher.json").read_text())
        self.assertEqual(config["ModID"], "FotG_R")
        self.assertTrue((portable / "Library").is_dir())
        self.assertEqual(self.run_helper()["args"], [])

    def test_completed_game_uses_direct_launch_and_keeps_runtime_local(self):
        self.install()
        self.add_game()
        result = self.run_helper()
        self.assertEqual(result["args"], ["-parse_cmdline_only", "-aa", "-soft_particles", "-deferred_cockpit", "-mod", "FotG_R-1.0.2"])
        self.assertEqual(result["cwd"], str(self.root / "launcher/kn_portable/Library/FotG_R"))
        self.assertEqual(result["cache"], str(self.root / "runtime-cache"))
        self.assertEqual(result["preferences"], str(self.root / "launcher/kn_portable/HardLightProductions/FotG_R") + os.sep)

    def test_interrupted_game_download_opens_launcher(self):
        self.install()
        folder = self.add_game()
        (folder / "knossos_net_download.token").touch()
        self.assertEqual(self.run_helper()["args"], [])

    def test_corrupt_manifest_opens_launcher(self):
        self.install()
        folder = self.add_game()
        (folder / "mod.json").write_text("{not finished")
        self.assertEqual(self.run_helper()["args"], [])

    def test_manage_action_opens_launcher_even_after_install(self):
        self.install()
        self.add_game()
        self.assertEqual(self.run_helper("--launcher")["args"], [])

    def test_rerun_preserves_library_preferences_and_newer_launcher(self):
        self.install()
        folder = self.add_game()
        save = folder / "pilot-save.plr"
        save.write_bytes(b"preserve pilot")
        preferences = self.root / "launcher" / "kn_portable" / "KnossosNET" / "settings.json"
        preferences.write_text('{"user_setting":"keep this"}')
        custom = preferences.with_name("custom_launcher.json")
        custom.write_text('{"ModID":"FotG_R","WindowTitle":"My launcher"}')
        binary = self.root / "launcher" / "Knossos.NET"
        binary.write_bytes(FAKE_BINARY + b"\n# newer launcher\n")
        helper = self.root / "launch-fate-of-the-galaxy.py"
        helper.write_text("# user's edited helper\n")
        before = {path: path.read_bytes() for path in (save, preferences, custom, binary)}
        self.urlopen.reset_mock()
        self.install()
        self.urlopen.assert_not_called()
        for path, original in before.items():
            self.assertEqual(path.read_bytes(), original)
        backups = list(self.root.glob("launch-fate-of-the-galaxy.py.bak-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), "# user's edited helper\n")

    def test_bad_checksum_leaves_no_install_or_application_entry(self):
        self.urlopen.side_effect = lambda *a, **k: Response(b"damaged download")
        with self.assertRaisesRegex(setup.SetupError, "verification failed|larger"):
            self.install()
        self.assertFalse(self.root.exists())
        self.assertFalse(self.apps.exists())
        self.assertEqual(list(self.root.parent.glob(".fotg-setup-*")), [])

    def test_interrupted_response_is_retried(self):
        self.urlopen.side_effect = [http.client.IncompleteRead(b"partial"), Response(self.archive)]
        self.install()
        self.assertEqual(self.urlopen.call_count, 2)
        self.assertTrue((self.root / "launcher" / "Knossos.NET").exists())

    def test_existing_unrelated_folder_is_untouched(self):
        self.root.mkdir(parents=True)
        original = self.root / "important.txt"
        original.write_bytes(b"not installer data")
        with self.assertRaisesRegex(setup.SetupError, "already contains"):
            self.install()
        self.assertEqual(original.read_bytes(), b"not installer data")
        self.assertEqual(list(self.root.iterdir()), [original])
        self.urlopen.assert_not_called()

    def test_unrelated_desktop_entry_is_not_replaced(self):
        self.apps.mkdir(parents=True)
        desktop = self.apps / setup.DESKTOP_ID
        desktop.write_text("[Desktop Entry]\nName=Another program\n")
        with self.assertRaisesRegex(setup.SetupError, "unrelated application"):
            self.install()
        self.assertIn("Another program", desktop.read_text())
        self.assertFalse(self.root.exists())
        self.urlopen.assert_not_called()

    def test_unsafe_archives_are_not_extracted(self):
        for name, member_type in (("../unrelated-file", tarfile.REGTYPE),
                                  ("Knossos.NET", tarfile.SYMTYPE)):
            with self.subTest(name=name, member_type=member_type):
                archive = self.home / "bad.tar.gz"
                archive.write_bytes(make_archive(name, member_type=member_type))
                destination = self.home / "stage"
                with self.assertRaisesRegex(setup.SetupError, "Unexpected launcher archive"):
                    setup.unpack_launcher(archive, destination)
                self.assertFalse((destination / "Knossos.NET").exists())
                self.assertFalse((self.home / "unrelated-file").exists())

    def test_desktop_exec_preserves_punctuation_without_shell_expansion(self):
        unusual = self.home / 'Games $HOME `whoami` %value \\ "quoted"' / "FotG"
        content = setup.desktop_content(unusual)
        exec_line = next(line[5:] for line in content.splitlines() if line.startswith("Exec="))
        escapes = {"\\": "\\", "s": " ", "n": "\n", "r": "\r", "t": "\t"}
        decoded = re.sub(r"\\([\\snrt])", lambda m: escapes[m[1]], exec_line)
        decoded = decoded.replace("%%", "%")
        # A shell provides an independent check of the spec's argument quoting.
        # printf receives literal arguments; no generated path is executed.
        result = subprocess.run(["/bin/bash", "-c", "printf '%s\\0' " + decoded],
                                capture_output=True, check=True)
        arguments = result.stdout.decode().rstrip("\0").split("\0")
        self.assertEqual(arguments, [sys.executable, str(unusual / "launch-fate-of-the-galaxy.py")])

    def test_no_launch_and_no_steam_do_not_start_processes(self):
        with mock.patch.object(setup, "preflight"), \
             mock.patch.object(setup.Path, "home", return_value=self.home), \
             mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(self.apps.parent)}), \
             mock.patch.object(setup.subprocess, "Popen") as start:
            self.assertEqual(setup.main(["--no-steam", "--no-launch"]), 0)
            start.assert_not_called()

    def test_full_install_passes_custom_location_to_companion(self):
        distribution = self.home / "extracted installer"
        distribution.mkdir()
        companion = distribution / "install-game-content.py"
        companion.write_text("# distribution fixture\n")
        with mock.patch.object(setup, "preflight"), \
             mock.patch.object(setup.Path, "home", return_value=self.home), \
             mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(self.apps.parent), "FOTG_INSTALLER_DIRECTORY": str(distribution)}), \
             mock.patch.object(setup.sys, "version_info", (3, 14)), \
             mock.patch.object(setup.subprocess, "run", return_value=types.SimpleNamespace(returncode=0)) as run, \
             mock.patch.object(setup.subprocess, "Popen") as start:
            self.assertEqual(setup.main(["--with-content", "--no-steam", "--no-launch", "--install-dir", str(self.root)]), 0)
            run.assert_called_once_with([sys.executable, str(companion), "--install-dir", str(self.root)])
            start.assert_not_called()

    def test_failed_content_install_does_not_register_or_launch(self):
        companion = self.home / "install-game-content.py"
        companion.touch()
        with mock.patch.object(setup, "preflight"), \
             mock.patch.object(setup.Path, "home", return_value=self.home), \
             mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(self.apps.parent), "FOTG_INSTALLER_DIRECTORY": str(self.home)}), \
             mock.patch.object(setup.sys, "version_info", (3, 14)), \
             mock.patch.object(setup.subprocess, "run", return_value=types.SimpleNamespace(returncode=1)), \
             mock.patch.object(setup, "request_steam_shortcut") as register, \
             mock.patch.object(setup.subprocess, "Popen") as start:
            with self.assertRaisesRegex(setup.SetupError, "Game installation stopped"):
                setup.main(["--with-content"])
            register.assert_not_called()
            start.assert_not_called()

    def test_steam_request_is_encoded_and_not_repeated_on_rerun(self):
        with mock.patch.object(setup, "preflight"), \
             mock.patch.object(setup.Path, "home", return_value=self.home), \
             mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(self.apps.parent)}), \
             mock.patch.object(setup.shutil, "which", return_value="/usr/bin/steam"), \
             mock.patch.object(setup.os, "open", wraps=os.open) as open_file, \
             mock.patch.object(setup.subprocess, "Popen") as start:
            # The sentinel is the only external write. Redirect just that open
            # into this temporary home; retain real file operations elsewhere.
            real_open = os.open
            def local_open(path, flags, mode=0o777, **kwargs):
                if path == "/tmp/addnonsteamgamefile":
                    path = str(self.home / "steam-sentinel")
                return real_open(path, flags, mode, **kwargs)
            # wraps has already captured the real function above.
            real_open = open_file._mock_wraps
            open_file.side_effect = local_open
            setup.main(["--no-launch"])
            command = start.call_args.args[0]
            self.assertEqual(command[0], "/usr/bin/steam")
            self.assertTrue(command[1].startswith("steam://addnonsteamgame/%2F"))
            self.assertIn("%20", command[1])
            setup.main(["--no-launch"])
            self.assertEqual(start.call_count, 1)

    def test_preflight_rejects_mac_and_root(self):
        with mock.patch.object(setup.sys, "platform", "darwin"):
            with self.assertRaisesRegex(setup.SetupError, "SteamOS machine"):
                setup.preflight()
        with mock.patch.object(setup.sys, "platform", "linux"), \
             mock.patch.object(setup.platform, "machine", return_value="x86_64"), \
             mock.patch.object(setup.os, "geteuid", return_value=0):
            with self.assertRaisesRegex(setup.SetupError, "without sudo"):
                setup.preflight()


if __name__ == "__main__":
    unittest.main(verbosity=2)
