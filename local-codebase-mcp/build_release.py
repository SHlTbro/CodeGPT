"""Create a clean, Git-free Core archive with a generated runtime identity manifest."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
import uuid
import zipfile
from pathlib import Path

from build_info import CORE_RELEASE, MANIFEST_NAME, utc_now

RELEASE_OMIT_PREFIXES = (
    "tests/",
    "docs/local-agent-v1-iteration-r",
)
QUICK_TUNNEL_ENDPOINT = re.compile(
    rb"https?://[a-z0-9-]+\.trycloudflare\.com(?=$|[^a-z0-9_.-]|\.(?=$|[^a-z0-9_-]))",
    re.IGNORECASE,
)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], check=True,
                            capture_output=True, text=True, timeout=15)
    return result.stdout.strip()


def package_release(root: Path | str, output: Path | str, *, ref: str = "HEAD",
                    build_id: str | None = None) -> dict:
    root, output = Path(root).resolve(), Path(output).resolve()
    git_root = Path(_git(root, "rev-parse", "--show-toplevel")).resolve()
    if os.path.normcase(str(git_root)) != os.path.normcase(str(root)):
        raise ValueError("release root must be the Git top-level directory")
    if _git(root, "status", "--porcelain", "--untracked-files=all"):
        raise ValueError("release packaging requires a clean worktree")
    commit = _git(root, "rev-parse", "--verify", f"{ref}^{{commit}}")
    if len(commit) != 40 or any(char not in "0123456789abcdefABCDEF" for char in commit):
        raise ValueError("Git returned an invalid source commit")
    build_id = build_id or uuid.uuid4().hex
    if not isinstance(build_id, str) or not build_id.strip():
        raise ValueError("build_id must be a non-empty string")
    manifest = {"product_version": CORE_RELEASE, "source_commit": commit.lower(),
                "build_id": build_id, "built_at_utc": utc_now(), "worktree_dirty": False}

    output.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_name = tempfile.mkstemp(prefix=f".{output.name}.source.", suffix=".tmp", dir=output.parent)
    os.close(fd)
    fd, staging_name = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
    os.close(fd)
    raw_archive, staging = Path(raw_name), Path(staging_name)
    try:
        subprocess.run(["git", "-C", str(root), "archive", "--format=zip", "--output", str(raw_archive), commit],
                       check=True, capture_output=True, text=True, timeout=60)
        with zipfile.ZipFile(raw_archive) as source, zipfile.ZipFile(
                staging, "w", compression=zipfile.ZIP_DEFLATED) as filtered:
            for info in source.infolist():
                name = info.filename.replace("\\", "/").lstrip("/")
                if any(name == prefix.rstrip("/") or name.startswith(prefix)
                       for prefix in RELEASE_OMIT_PREFIXES):
                    continue
                filtered.writestr(info, source.read(info.filename))
        with zipfile.ZipFile(staging, "a", compression=zipfile.ZIP_DEFLATED) as archive:
            if MANIFEST_NAME in archive.namelist():
                raise ValueError(f"source commit already contains generated {MANIFEST_NAME}")
            archive.writestr(MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        with zipfile.ZipFile(staging) as archive:
            names = archive.namelist()
            if MANIFEST_NAME not in names or any(name.startswith(".git/") for name in names):
                raise ValueError("release archive verification failed")
            if any(any(name.startswith(prefix) for prefix in RELEASE_OMIT_PREFIXES) for name in names):
                raise ValueError("development-only files were included in the release archive")
            if any(QUICK_TUNNEL_ENDPOINT.search(archive.read(name))
                   for name in names if not name.endswith("/")):
                raise ValueError("release archive contains a Quick Tunnel endpoint")
        os.replace(staging, output)
    finally:
        raw_archive.unlink(missing_ok=True)
        staging.unlink(missing_ok=True)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Package a clean CodeGPT Core release without .git metadata.")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ref", default="HEAD", help="Git commit or tag to package")
    args = parser.parse_args()
    try:
        manifest = package_release(args.root, args.output, ref=args.ref)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        parser.error(f"release archive was not created: {exc}")
    print(json.dumps({"artifact": str(args.output.resolve()), **manifest}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
