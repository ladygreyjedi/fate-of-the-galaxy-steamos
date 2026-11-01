"""Integrity, extraction, and preservation checks with synthetic local archives."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import types
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location("content_installer", Path(__file__).with_name("install-game-content.py"))
content = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(content)


def archive_bytes(filename, value, kind=tarfile.REGTYPE):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        entry = tarfile.TarInfo(filename)
        entry.type = kind
        entry.mode = 0o755
        entry.size = len(value) if kind == tarfile.REGTYPE else 0
        if kind == tarfile.SYMTYPE:
            entry.linkname = "../../outside"
        archive.addfile(entry, io.BytesIO(value) if kind == tarfile.REGTYPE else None)
    return output.getvalue()


def fixture_mod(identifier, version, filename, value, archive):
    engine = identifier == "FSO"
    return {"id": identifier, "title": identifier, "version": version, "type": "engine" if engine else "tc",
            "parent": None, "mod_flag": [] if engine else [identifier], "cmdline": "",
            "packages": [{"name": "fixture", "status": "required", "folder": None,
                          "environment": "linux && x86_64" if engine else None,
                          "dependencies": [] if engine else [{"id": "FSO", "version": "26.0.1"}],
                          "executables": [{"file": filename, "label": None}] if engine else [],
                          "files": [{"filename": identifier + ".tar.gz", "dest": "", "filesize": len(archive),
                                     "checksum": ["sha256", hashlib.sha256(archive).hexdigest()],
                                     "urls": ["https://example.invalid/" + identifier]}],
                          "filelist": [{"filename": filename, "checksum": ["sha256", hashlib.sha256(value).hexdigest()]}]}]}


class ContentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="fotg-content-tests-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.root = self.home / "Games/FotG"
        self.work = self.root / ".content-install"
        self.library = self.root / "launcher/kn_portable/Library"
        self.root.mkdir(parents=True)
        (self.root / ".fotg-steamos-install.json").write_text('{"owner":"fotg-steamos-installer-v1"}')
        for name, value in (("ROOT", self.root), ("WORK", self.work), ("LIBRARY", self.library), ("ACTIVE_INSTALL", False)):
            patch = mock.patch.object(content, name, value)
            patch.start(); self.addCleanup(patch.stop)
        self.game_value = b"verified game data"
        self.engine_value = b"verified engine fixture"
        self.game_archive = archive_bytes("data/game.tbl", self.game_value)
        self.engine_archive = archive_bytes("game.AppImage", self.engine_value)
        self.game = fixture_mod("FotG_R", "1.0.2", "data/game.tbl", self.game_value, self.game_archive)
        self.engine = fixture_mod("FSO", "26.0.1", "game.AppImage", self.engine_value, self.engine_archive)
        self.stdout = contextlib.redirect_stdout(io.StringIO())
        self.stdout.__enter__(); self.addCleanup(self.stdout.__exit__, None, None, None)

    def pinned(self, mod):
        payload = content.release_payload(mod)
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()
        return mock.patch.dict(content.RELEASES, {mod["id"]: {"version": mod["version"], "sha256": digest}})

    def test_changed_archive_metadata_is_rejected(self):
        with self.pinned(self.game):
            self.game["packages"][0]["files"][0]["checksum"][1] = "0" * 64
            with self.assertRaisesRegex(RuntimeError, "metadata changed"):
                content.validate_manifest(self.game, "FotG_R")

    def test_description_changes_do_not_change_the_release_pin(self):
        with self.pinned(self.game):
            self.game["description"] = "Updated release description"
            self.assertEqual(content.validate_manifest(self.game, "FotG_R")["version"], "1.0.2")

    def test_invalid_metadata_is_not_cached(self):
        self.work.mkdir()
        response = io.BytesIO(json.dumps({"mod": self.game}).encode())
        response.geturl = lambda: "https://example.invalid/metadata"
        with mock.patch.object(content.urllib.request, "urlopen", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "metadata changed"):
                content.load_manifest("FotG_R")
        self.assertFalse((self.work / "manifests").exists())

    def test_insecure_metadata_redirect_is_rejected(self):
        response = io.BytesIO(b"{}")
        response.geturl = lambda: "http://example.invalid/metadata"
        with mock.patch.object(content.urllib.request, "urlopen", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "away from HTTPS"):
                content.load_manifest("FotG_R")

    def test_unsafe_paths_are_rejected(self):
        for value in ("../outside", "/outside", "data/../../outside", "C:\\outside"):
            with self.subTest(path=value), self.assertRaises(RuntimeError):
                content.safe_path(self.root, value)

    def test_archive_symlink_is_not_extracted(self):
        path = self.root / "unsafe.tar.gz"
        path.write_bytes(archive_bytes("link", b"", tarfile.SYMTYPE))
        task = self.engine, self.engine["packages"][0], {"filename": "unsafe.tar.gz", "dest": ""}
        with self.assertRaisesRegex(RuntimeError, "link or special file"):
            content.extract(task, path, self.root / "stage")
        self.assertFalse((self.home / "outside").exists())

    def test_corrupt_download_is_never_reused(self):
        (self.work / "downloads").mkdir(parents=True)
        package = self.game["packages"][0]
        archive = package["files"][0]
        def damaged(command, **kwargs):
            Path(command[command.index("--output") + 1]).write_bytes(b"x" * archive["filesize"])
            return types.SimpleNamespace(returncode=0)
        with mock.patch.object(content.subprocess, "run", side_effect=damaged):
            with self.assertRaisesRegex(RuntimeError, "mirrors failed"):
                content.download((self.game, package, archive))
        self.assertEqual(list((self.work / "downloads").iterdir()), [])

    def test_valid_download_is_reused_without_network(self):
        (self.work / "downloads").mkdir(parents=True)
        package = self.game["packages"][0]
        archive = package["files"][0]
        target = self.work / "downloads" / (archive["checksum"][1] + "-" + archive["filename"])
        target.write_bytes(self.game_archive)
        with mock.patch.object(content.subprocess, "run") as network:
            self.assertEqual(content.download((self.game, package, archive))[1], target)
            network.assert_not_called()

    def test_bad_file_hash_does_not_mark_installation_complete(self):
        stage = self.root / "stage"
        (stage / "data").mkdir(parents=True)
        (stage / "data/game.tbl").write_bytes(b"corrupt")
        with self.assertRaisesRegex(RuntimeError, "failed SHA-256"):
            content.verify(self.game, stage)
        self.assertFalse((stage / "mod.json").exists())

    def fake_curl(self, command, **kwargs):
        values = {"https://example.invalid/FotG_R": self.game_archive, "https://example.invalid/FSO": self.engine_archive}
        Path(command[command.index("--output") + 1]).write_bytes(values[command[-1]])
        return types.SimpleNamespace(returncode=0)

    def run_install(self):
        with mock.patch.object(content.sys, "platform", "linux"), \
             mock.patch.object(content.platform, "machine", return_value="x86_64"), \
             mock.patch.object(content.os, "geteuid", return_value=1000), \
             mock.patch.object(content.Path, "home", return_value=self.home), \
             mock.patch.object(content.shutil, "which", return_value="/usr/bin/tool"), \
             mock.patch.object(content.shutil, "disk_usage", return_value=types.SimpleNamespace(free=20 * 1024**3)), \
             mock.patch.object(content, "load_manifest", side_effect=[self.game, self.engine]), \
             mock.patch.object(content.subprocess, "run", side_effect=self.fake_curl):
            content.main(["--install-dir", str(self.root)])

    def test_full_install_verifies_game_and_engine_before_publishing(self):
        self.run_install()
        game = self.library / "FotG_R/FotG_R-1.0.2"
        engine = self.library / "bin/FSO-26.0.1"
        self.assertEqual((game / "data/game.tbl").read_bytes(), self.game_value)
        self.assertEqual((engine / "game.AppImage").read_bytes(), self.engine_value)
        self.assertTrue(os.access(engine / "game.AppImage", os.X_OK))
        self.assertTrue(json.loads((game / "mod.json").read_text())["installed"])
        receipt = json.loads((self.work / "receipt.json").read_text())
        self.assertTrue(receipt["complete"])
        self.assertEqual(receipt["verified_file_counts"], {"FotG_R": 1, "FSO": 1})
        self.assertEqual(list((self.work / "downloads").iterdir()), [])

    def test_bad_engine_file_leaves_both_releases_unpublished(self):
        self.engine["packages"][0]["filelist"][0]["checksum"][1] = "0" * 64
        with self.assertRaisesRegex(RuntimeError, "failed SHA-256"):
            self.run_install()
        self.assertFalse((self.library / "FotG_R/FotG_R-1.0.2").exists())
        self.assertFalse((self.library / "bin/FSO-26.0.1").exists())

    def test_existing_game_files_are_preserved(self):
        target = self.library / "FotG_R/FotG_R-1.0.2"
        target.mkdir(parents=True)
        save = target / "save.plr"
        save.write_bytes(b"original")
        with self.assertRaisesRegex(RuntimeError, "refusing to overwrite"):
            self.run_install()
        self.assertEqual(save.read_bytes(), b"original")


if __name__ == "__main__":
    unittest.main(verbosity=2)
