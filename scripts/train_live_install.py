#!/usr/bin/env python3
"""Explicit, local-only installer for the verified Joooook/12306-mcp commit.

Nothing is installed globally and no package is fetched by the query script.
The destination must be a new path. The source archive, package lock, one audited
source patch and upstream MIT licence are recorded in INSTALLATION.json.

  python3 scripts/train_live_install.py --prefix "$HOME/.local/share/dsh-trip-planner/12306-mcp"

Then configure scripts/train_live.py with:
  --mcp-command 'node /absolute/prefix/build/index.js'
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.request import urlopen

from train_live import UPSTREAM_COMMIT, UPSTREAM_REPOSITORY, UPSTREAM_VERSION

ARCHIVE_SHA256 = "8a780cbb89b515382955b68d5d50a2f5b6f888562601eba854a67546b3a6becf"
PATCH_NAME = "0001-axios-timeout.patch"
PATCH_PATH = Path(__file__).resolve().parent / "train_live_patches" / PATCH_NAME
PATCH_FIND = "        const response = await axios.get(url + '?' + scheme.toString(), {\n            headers: headers,\n        });"
PATCH_REPLACE = "        const response = await axios.get(url + '?' + scheme.toString(), {\n            timeout: 10000,\n            headers: headers,\n        });"


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def download_archive(destination):
    url = "https://codeload.github.com/{}/tar.gz/{}".format(UPSTREAM_REPOSITORY, UPSTREAM_COMMIT)
    last_error = None
    for attempt in range(2):
        try:
            with urlopen(url, timeout=30) as response, destination.open("wb") as output:
                shutil.copyfileobj(response, output)
            if sha256(destination) != ARCHIVE_SHA256:
                raise RuntimeError("upstream archive SHA-256 mismatch")
            return
        except Exception as error:
            last_error = error
            if attempt == 0:
                time.sleep(1)
    raise RuntimeError("cannot download the pinned upstream archive: {}".format(last_error))


def safe_extract(archive, destination):
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        for member in members:
            path = Path(member.name)
            if path.is_absolute() or ".." in path.parts or member.issym() or member.islnk():
                raise RuntimeError("unsafe path in upstream archive")
        bundle.extractall(destination)
    roots = [p for p in destination.iterdir() if p.is_dir()]
    if len(roots) != 1:
        raise RuntimeError("upstream archive does not have one root")
    return roots[0]


def apply_timeout_patch(source_root):
    target = source_root / "src" / "index.ts"
    text = target.read_text(encoding="utf-8")
    if text.count(PATCH_FIND) != 1:
        raise RuntimeError("audited timeout patch does not match the pinned source")
    target.write_text(text.replace(PATCH_FIND, PATCH_REPLACE), encoding="utf-8")


def check_runtime():
    if shutil.which("node") is None or shutil.which("npm") is None:
        raise RuntimeError("Node.js >= 18 and npm are required; no runtime was installed")
    version = subprocess.check_output(["node", "--version"], text=True).strip()
    match = re.search(r"v(\d+)", version)
    if not match or int(match.group(1)) < 18:
        raise RuntimeError("Node.js >= 18 is required (found {})".format(version))


def run(command, cwd, timeout):
    try:
        subprocess.run(command, cwd=cwd, timeout=timeout, check=True)
    except subprocess.TimeoutExpired:
        raise RuntimeError("install command timed out: {}".format(command[0]))
    except subprocess.CalledProcessError:
        raise RuntimeError("install command failed: {}".format(command[0]))


def install(prefix):
    check_runtime()
    destination = Path(prefix).expanduser().resolve()
    if destination.exists():
        raise RuntimeError("destination already exists; refusing to overwrite: {}".format(destination))
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="dsh-train-live-install-") as temporary:
        work = Path(temporary)
        archive = work / "upstream.tar.gz"
        download_archive(archive)
        extracted = safe_extract(archive, work / "source")
        package = json.loads((extracted / "package.json").read_text(encoding="utf-8"))
        if package.get("name") != "12306-mcp" or package.get("version") != UPSTREAM_VERSION:
            raise RuntimeError("upstream package/version does not match the verified schema")
        if package.get("license") != "MIT":
            raise RuntimeError("upstream licence changed; refusing unreviewed installation")
        apply_timeout_patch(extracted)
        patch_record = {
            "file": PATCH_NAME,
            "target": "src/index.ts",
            "purpose": "10s timeout for each upstream Axios request; MCP retries stay in train_live.py",
            "sha256": sha256(PATCH_PATH),
        }
        # No lifecycle scripts: build explicitly only after dependencies are local.
        run(["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"], extracted, 300)
        run(["npm", "run", "build"], extracted, 120)
        notices = extracted / "dsh-installation"
        notices.mkdir()
        shutil.copy2(PATCH_PATH, notices / PATCH_NAME)
        record = {
            "repository": UPSTREAM_REPOSITORY,
            "commit": UPSTREAM_COMMIT,
            "package_version": UPSTREAM_VERSION,
            "archive_sha256": ARCHIVE_SHA256,
            "dependency_lock": "package-lock.json (npm ci)",
            "patches": [patch_record],
            "license": {
                "spdx": "MIT",
                "file": "LICENSE",
                "sha256": sha256(extracted / "LICENSE"),
                "copyright": "Copyright (c) 2025 Jok",
            },
            "scope": "local stdio MCP only; public read queries; no login/booking/passenger data",
        }
        (notices / "INSTALLATION.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        shutil.copytree(extracted, destination)
    print("Installed local MCP server: {}".format(destination))
    print("Use --mcp-command 'node {}'".format(destination / "build" / "index.js"))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--prefix", required=True, help="new local installation directory; never global")
    args = parser.parse_args()
    try:
        install(args.prefix)
    except (OSError, RuntimeError, json.JSONDecodeError) as error:
        print("install failed: {}".format(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
