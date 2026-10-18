#!/usr/bin/env python3
"""Download the pinned CI tools from ci/tools.lock.json and verify SHA-256.

    python3 ci/fetch_tools.py [--dest build/ci-tools] [gcc renode svd-stm32g474]

Archives are cached in <dest>/downloads; a file whose hash does not match is
deleted and the run fails. Each tool is unpacked into <dest>/<name>, and the
directory is reused while its .sha256 marker matches the lock file. A "git" entry
is fetched at the pinned commit (the commit hash is the integrity check).
"""
import argparse
import hashlib
import json
import pathlib
import shutil
import subprocess
import sys
import tarfile
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
LOCK = ROOT / "ci" / "tools.lock.json"


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(url, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".part")
    with urllib.request.urlopen(url, timeout=120) as response, partial.open("wb") as out:
        shutil.copyfileobj(response, out)
    partial.replace(target)


def safe_extract(archive, dest, strip):
    with tarfile.open(archive) as tar:
        for member in tar.getmembers():
            parts = pathlib.PurePosixPath(member.name).parts[strip:]
            if not parts or ".." in parts or member.name.startswith("/"):
                continue
            member.name = str(pathlib.PurePosixPath(*parts))
            if member.issym() and pathlib.PurePosixPath(member.linkname).is_absolute():
                continue  # relative symlinks only (xPack uses them inside bin/ and lib/)
            if member.islnk():
                link = pathlib.PurePosixPath(member.linkname).parts[strip:]
                if not link or ".." in link:
                    continue
                member.linkname = str(pathlib.PurePosixPath(*link))
            tar.extract(member, dest)


def fetch_git(name, spec, dest):
    """Shallow fetch of one commit; the checkout must resolve to exactly that commit."""
    target = dest / name
    marker = dest / (name + ".commit")
    if marker.is_file() and marker.read_text().strip() == spec["commit"] and target.is_dir():
        print("%-14s %s (cached)" % (name, spec["version"]))
        return target
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    git = ["git", "-C", str(target), "-c", "advice.detachedHead=false"]
    subprocess.run(git[:3] + ["init", "-q"], check=True)
    print("%-14s fetching %s@%s" % (name, spec["url"], spec["commit"][:12]))
    subprocess.run(git + ["fetch", "-q", "--depth", "1", spec["url"], spec["commit"]], check=True)
    subprocess.run(git + ["checkout", "-q", "FETCH_HEAD"], check=True)
    head = subprocess.run(git + ["rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    if head != spec["commit"]:
        raise SystemExit("%s: fetched %s, expected %s" % (name, head, spec["commit"]))
    marker.write_text(spec["commit"] + "\n")
    print("%-14s %s -> %s" % (name, spec["version"], target))
    return target


def fetch(name, spec, dest):
    if spec["kind"] == "git":
        return fetch_git(name, spec, dest)
    marker = dest / name / ".sha256"
    if marker.is_file() and marker.read_text().strip() == spec["sha256"]:
        print("%-14s %s (cached)" % (name, spec["version"]))
        return dest / name
    archive = dest / "downloads" / spec["url"].rsplit("/", 1)[-1]
    if not archive.is_file() or sha256(archive) != spec["sha256"]:
        print("%-14s downloading %s" % (name, spec["url"]))
        download(spec["url"], archive)
    actual = sha256(archive)
    if actual != spec["sha256"]:
        archive.unlink()
        raise SystemExit("%s: SHA-256 mismatch: %s, expected %s" % (name, actual, spec["sha256"]))
    target = dest / name
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    if spec["kind"] == "tar":
        safe_extract(archive, target, spec.get("strip", 0))
    else:
        shutil.copy2(archive, target / spec.get("name", archive.name))
    marker.write_text(spec["sha256"] + "\n")
    print("%-14s %s -> %s" % (name, spec["version"], target))
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dest", type=pathlib.Path, default=ROOT / "build" / "ci-tools")
    parser.add_argument("tools", nargs="*", help="names from the lock file (default: all)")
    args = parser.parse_args(argv)
    lock = json.loads(LOCK.read_text(encoding="utf-8"))["tools"]
    unknown = set(args.tools) - set(lock)
    if unknown:
        parser.error("unknown tools: %s" % ", ".join(sorted(unknown)))
    for name in args.tools or sorted(lock):
        fetch(name, lock[name], args.dest.resolve())
    return 0


if __name__ == "__main__":
    sys.exit(main())
