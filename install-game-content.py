#!/usr/bin/env python3
"""Install the verified FotG 1.0.2 Nebula packages and its exact Linux engine."""
import concurrent.futures
import argparse
import fcntl
import hashlib
import json
import os
import platform
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.parse
import urllib.request

ROOT = Path.home() / "Games/Fate-of-the-Galaxy"
WORK = ROOT / ".content-install"
LIBRARY = ROOT / "launcher/kn_portable/Library"
ACTIVE_INSTALL = False
RELEASES = {
    "FotG_R": {"version": "1.0.2", "sha256": "1b479754ad030bb4221bca590364c870ca4a23187672c701c455641f625f1e05"},
    "FSO": {"version": "26.0.1", "sha256": "47f4ab5e8e91b275c34ab80f3deae7b6922067b5a9b13017ad9f71dca7e7fe92"},
}


def release_payload(mod):
    """Pin installation data; descriptions and screenshots do not affect a release."""
    fields = ("id", "title", "version", "type", "parent", "stability", "mod_flag", "cmdline", "custom_build")
    payload = {key: mod.get(key) for key in fields}
    package_fields = ("name", "status", "environment", "folder", "is_vp", "dependencies", "executables", "files", "filelist")
    payload["packages"] = [{key: package.get(key) for key in package_fields}
                           for package in mod["packages"]
                           if mod["type"] != "engine" or package.get("environment") == "linux && x86_64"]
    return payload


def validate_manifest(mod, identifier):
    release = RELEASES[identifier]
    payload = release_payload(mod)
    actual = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()
    if payload["id"] != identifier or payload["version"] != release["version"] or actual != release["sha256"]:
        raise RuntimeError("The published installation metadata changed for " + identifier
                           + "; no content was installed. Check the repository for an updated installer.")
    return payload


def load_manifest(identifier):
    release = RELEASES[identifier]
    cache = WORK / "manifests" / (identifier + "-" + release["version"] + ".json")
    if cache.exists():
        return validate_manifest(json.loads(cache.read_text()), identifier)
    url = "https://api.fsnebula.org/api/1/mod/json/" + identifier + "/" + release["version"]
    request = urllib.request.Request(url, headers={"User-Agent": "FotG-SteamOS-Installer/0.1.0"})
    with urllib.request.urlopen(request, timeout=60) as response:
        if urllib.parse.urlparse(response.geturl()).scheme != "https":
            raise RuntimeError("The metadata download was redirected away from HTTPS.")
        data = response.read(16 * 1024 * 1024 + 1)
        if len(data) > 16 * 1024 * 1024:
            raise RuntimeError("The metadata response was unexpectedly large.")
    payload = validate_manifest(json.loads(data)["mod"], identifier)
    cache.parent.mkdir(exist_ok=True)
    cache.write_text(json.dumps(payload) + "\n")
    return payload


def report(state, **details):
    value = {"state": state, "updated": time.time(), **details}
    temporary = WORK / "status.tmp"
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(WORK / "status.json")
    print(json.dumps(value), flush=True)


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def safe_path(base, relative):
    relative = relative.replace("\\", "/")
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts or ":" in relative:
        raise RuntimeError("Unsafe archive path: " + relative)
    result = base.joinpath(*path.parts)
    if not result.resolve().is_relative_to(base.resolve()):
        raise RuntimeError("Archive path escapes staging directory.")
    return result


def download(task):
    mod, package, archive = task
    algorithm, expected = archive["checksum"]
    if algorithm.lower() != "sha256" or len(expected) != 64:
        raise RuntimeError("A package is missing a SHA-256 checksum.")
    target = WORK / "downloads" / (expected + "-" + Path(archive["filename"]).name)
    if target.exists() and target.stat().st_size == archive["filesize"] and digest(target) == expected:
        print("Reusing verified " + archive["filename"], flush=True)
        return task, target
    partial = target.with_name(target.name + ".part")
    print("Downloading " + archive["filename"] + " (" + str(archive["filesize"] // 1048576) + " MiB)", flush=True)
    for url in archive["urls"]:
        if urllib.parse.urlparse(url).scheme != "https":
            continue
        result = subprocess.run([
            "curl", "--fail", "--location", "--silent", "--show-error",
            "--proto", "=https", "--proto-redir", "=https",
            "--connect-timeout", "20", "--max-time", "900", "--retry", "2",
            "--continue-at", "-", "--output", str(partial), url,
        ], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if result.returncode == 0 and partial.exists() and partial.stat().st_size == archive["filesize"]:
            if digest(partial) == expected:
                partial.replace(target)
                print("SHA-256 verified: " + archive["filename"], flush=True)
                return task, target
        # This is only this installer's incomplete download; try the next mirror afresh.
        partial.unlink(missing_ok=True)
    raise RuntimeError("All download mirrors failed for " + archive["filename"])


def extract(task, archive_path, stage):
    mod, package, archive = task
    destination = safe_path(stage, archive.get("dest") or "")
    destination.mkdir(parents=True, exist_ok=True)
    print("Extracting " + archive["filename"], flush=True)
    if archive["filename"].endswith(".tar.gz"):
        with tarfile.open(archive_path, "r:gz") as source:
            for member in source.getmembers():
                safe_path(destination, member.name)
                if not (member.isfile() or member.isdir()):
                    raise RuntimeError("Unexpected link or special file in engine archive.")
            source.extractall(destination, filter="data")
    else:
        listing = subprocess.run(["7z", "l", "-slt", str(archive_path)], capture_output=True, text=True, check=True)
        entries = listing.stdout.split("----------", 1)
        if len(entries) != 2:
            raise RuntimeError("Could not inspect archive paths.")
        for line in entries[1].splitlines():
            if line.startswith("Path = "):
                safe_path(destination, line[7:])
            if line.startswith(("Symbolic Link = ", "Hard Link = ")):
                raise RuntimeError("Unexpected archive link.")
            if line.startswith("Attributes = ") and any(word.startswith("l") for word in line[13:].split()):
                raise RuntimeError("Unexpected archive symlink.")
        result = subprocess.run(["7z", "x", "-y", "-bd", "-bso0", "-bsp0", "-o" + str(destination), str(archive_path)], capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError("Extraction failed for " + archive["filename"])


def verify(mod, stage):
    count = 0
    for package in mod["packages"]:
        for entry in package.get("filelist", []):
            path = safe_path(stage, entry["filename"])
            if not path.is_file() or path.is_symlink():
                raise RuntimeError("Installed file is missing: " + entry["filename"])
            checksum = entry.get("checksum")
            if not checksum or checksum[0].lower() != "sha256" or digest(path) != checksum[1]:
                raise RuntimeError("Installed file failed SHA-256 verification: " + entry["filename"])
            count += 1
        for executable in package.get("executables", []):
            path = safe_path(stage, executable["file"])
            if not path.is_file():
                raise RuntimeError("The game engine executable is missing.")
            path.chmod(path.stat().st_mode | 0o111)
        package["isEnabled"] = True
    mod["installed"] = True
    mod["dev_mode"] = False
    (stage / "mod.json").write_text(json.dumps(mod, indent=2) + "\n")
    return count


def main(argv=None):
    global ROOT, WORK, LIBRARY, ACTIVE_INSTALL
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install-dir", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    if sys.platform != "linux" or platform.machine().lower() not in ("x86_64", "amd64") or os.geteuid() == 0:
        raise RuntimeError("Run as the normal user on x86_64 SteamOS, without sudo.")
    if sys.version_info < (3, 12):
        raise RuntimeError("Python 3.12 or newer is required.")
    ROOT = args.install_dir.expanduser().resolve()
    if ROOT == Path.home().resolve() or not ROOT.is_relative_to(Path.home().resolve()):
        raise RuntimeError("Choose an installation folder inside your home folder.")
    WORK = ROOT / ".content-install"
    LIBRARY = ROOT / "launcher/kn_portable/Library"
    for tool in ("curl", "7z"):
        if not shutil.which(tool):
            raise RuntimeError(tool + " is required; no system packages were changed.")
    marker = ROOT / ".fotg-steamos-install.json"
    if json.loads(marker.read_text()).get("owner") != "fotg-steamos-installer-v1":
        raise RuntimeError("Run the launcher installer first.")
    WORK.mkdir(exist_ok=True)
    with (WORK / "install.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        ACTIVE_INSTALL = True
        install_content()


def install_content():
    (WORK / "downloads").mkdir(exist_ok=True)
    game = load_manifest("FotG_R")
    engine = load_manifest("FSO")
    if (game["id"], game["version"], engine["id"], engine["version"]) != ("FotG_R", "1.0.2", "FSO", "26.0.1"):
        raise RuntimeError("Unexpected package manifest identity.")
    engine["packages"] = [p for p in engine["packages"] if p.get("environment") == "linux && x86_64"]
    if len(engine["packages"]) != 1 or any(p.get("status") != "required" or p.get("environment") for p in game["packages"]):
        raise RuntimeError("Unexpected package selection; review the manifests.")
    destinations = {
        "FotG_R": LIBRARY / "FotG_R/FotG_R-1.0.2",
        "FSO": LIBRARY / "bin/FSO-26.0.1",
    }
    if (WORK / "receipt.json").exists():
        receipt = json.loads((WORK / "receipt.json").read_text())
        if receipt.get("complete") and all((p / "mod.json").exists() for p in destinations.values()):
            report("complete", **receipt)
            return
    if any(path.exists() for path in destinations.values()):
        raise RuntimeError("Existing content was preserved; refusing to overwrite an installation.")
    if shutil.disk_usage(ROOT).free < 12 * 1024 ** 3:
        raise RuntimeError("At least 12 GiB free space is required for staging and verification.")
    mods = [game, engine]
    tasks = [(mod, package, archive) for mod in mods for package in mod["packages"] for archive in package["files"]]
    total = sum(t[2]["filesize"] for t in tasks)
    report("downloading", archive_count=len(tasks), total_download_bytes=total)
    downloaded = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        for result in pool.map(download, tasks):
            downloaded.append(result)
    report("extracting", archive_count=len(downloaded))
    stages = {mod["id"]: WORK / ("stage-" + mod["id"]) for mod in mods}
    for stage in stages.values():
        stage.mkdir(exist_ok=True)
    for task, path in downloaded:
        extract(task, path, stages[task[0]["id"]])
    report("verifying", archive_count=len(downloaded))
    counts = {mod["id"]: verify(mod, stages[mod["id"]]) for mod in mods}
    # Publish only after every game and engine file has passed its catalog checksum.
    published = []
    try:
        for mod in mods:
            target = destinations[mod["id"]]
            target.parent.mkdir(parents=True, exist_ok=True)
            stages[mod["id"]].rename(target)
            published.append((target, stages[mod["id"]]))
    except OSError:
        for target, stage in reversed(published):
            target.rename(stage)
        raise
    receipt = {"complete": True, "game": "FotG_R", "game_version": "1.0.2", "engine": "FSO", "engine_version": "26.0.1",
               "archive_count": len(downloaded), "archive_sha256_verified": True, "verified_file_counts": counts,
               "total_download_bytes": total, "installed_paths": {k: str(v) for k, v in destinations.items()}}
    (WORK / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    report("complete", **receipt)
    # Remove only the checksum-verified archives produced by this installer.
    for _, path in downloaded:
        path.unlink()


if __name__ == "__main__":
    try:
        main()
    except BlockingIOError:
        print("Another content installation is already running.", file=sys.stderr)
        raise SystemExit(1)
    except Exception as error:
        if ACTIVE_INSTALL:
            report("failed", error=str(error))
        else:
            print(str(error), file=sys.stderr)
        raise SystemExit(1)
