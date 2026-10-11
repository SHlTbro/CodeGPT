import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from build_release import package_release
from build_info import read_build_manifest


def git(directory, *args):
    return subprocess.run(["git", "-C", str(directory), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def test_release_archive_contains_frozen_manifest_and_no_git_metadata(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "release-test@example.invalid")
    git(repo, "config", "user.name", "Release Test")
    (repo / "setup.py").write_text("pass\n", encoding="utf-8")
    git(repo, "add", "setup.py")
    git(repo, "commit", "-m", "fixture")
    expected_commit = git(repo, "rev-parse", "HEAD")

    artifact = tmp_path / "CodeGPT.zip"
    result = package_release(repo, artifact, build_id="build-fixture")

    assert result["source_commit"] == expected_commit
    assert result["build_id"] == "build-fixture"
    with zipfile.ZipFile(artifact) as archive:
        names = archive.namelist()
        assert "build-manifest.json" in names
        assert not any(name.startswith(".git/") for name in names)
        manifest = json.loads(archive.read("build-manifest.json"))
        assert manifest["product_version"] == "1.2.0"
        assert manifest["source_commit"] == expected_commit
        assert manifest["build_id"] == "build-fixture"

    unpacked = tmp_path / "unpacked"
    unpacked.mkdir()
    with zipfile.ZipFile(artifact) as archive:
        archive.extractall(unpacked)
    loaded = read_build_manifest(unpacked)
    assert loaded["source_commit"] == manifest["source_commit"]
    assert loaded["build_id"] == manifest["build_id"]
    assert loaded["manifest_state"] == "valid"


def test_release_archive_omits_development_tests_and_internal_iteration_records(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "release-test@example.invalid")
    git(repo, "config", "user.name", "Release Test")
    files = {
        "setup.py": "pass\n",
        "README.md": "Install CodeGPT Core here.\n",
        "tests/unit/test_private_fixture.py": "assert True\n",
        "docs/local-agent-v1-iteration-r2/EXECUTION-REPORT.md": "https://old-session.trycloudflare.com/mcp\n",
        "docs/local-agent-v1-iteration-r3/EXECUTION-REPORT.md": "https://old-session.trycloudflare.com/mcp\n",
        "docs/local-agent-v1-iteration-r4/EXECUTION-REPORT.md": "https://old-session.trycloudflare.com/mcp\n",
        "docs/local-agent-v1-iteration-r6/EXECUTION-REPORT.md": "https://private-candidate.example.invalid/mcp\n",
        "docs/local-agent-v1-iteration-r7/EXECUTION-REPORT.md": "https://private-candidate.example.invalid/mcp\n",
        "docs/local-agent-v1-iteration-r4/state.json": "{}\n",
    }
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "fixture")

    artifact = tmp_path / "CodeGPT.zip"
    package_release(repo, artifact, build_id="filtered-build")

    with zipfile.ZipFile(artifact) as archive:
        names = set(archive.namelist())
    assert "setup.py" in names and "README.md" in names and "build-manifest.json" in names
    assert not any(name.startswith("tests/") for name in names)
    assert not any(name.startswith("docs/local-agent-v1-iteration-r") for name in names)
    with zipfile.ZipFile(artifact) as archive:
        packaged_text = b"".join(archive.read(name) for name in names if not name.endswith("/"))
        assert b"trycloudflare.com" not in packaged_text
        assert b"private-candidate.example.invalid/mcp" not in packaged_text


@pytest.mark.parametrize("endpoint", [
    "https://unlisted-session.trycloudflare.com/mcp",
    "https://unlisted-session.trycloudflare.com",
    "https://unlisted-session.trycloudflare.com?source=old",
    "https://unlisted-session.trycloudflare.com#history",
    '"https://unlisted-session.trycloudflare.com"',
    "`https://unlisted-session.trycloudflare.com`",
    "https://unlisted-session.trycloudflare.com;",
    "https://unlisted-session.trycloudflare.com.",
    "https://unlisted-session.trycloudflare.com./mcp",
])
def test_release_packaging_rejects_quick_tunnel_endpoint_outside_internal_records(tmp_path, endpoint):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "release-test@example.invalid")
    git(repo, "config", "user.name", "Release Test")
    (repo / "setup.py").write_text("pass\n", encoding="utf-8")
    (repo / "README.md").write_text(endpoint + "\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "fixture")

    with pytest.raises(ValueError, match="Quick Tunnel endpoint"):
        package_release(repo, tmp_path / "CodeGPT.zip", build_id="endpoint-build")


def test_release_packaging_does_not_treat_a_lookalike_domain_as_quick_tunnel(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "release-test@example.invalid")
    git(repo, "config", "user.name", "Release Test")
    (repo / "setup.py").write_text("pass\n", encoding="utf-8")
    (repo / "README.md").write_text("https://unlisted-session.trycloudflare.com.evil.invalid/mcp\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "fixture")

    package_release(repo, tmp_path / "CodeGPT.zip", build_id="lookalike-build")


def test_release_packaging_rejects_dirty_worktree(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "release-test@example.invalid")
    git(repo, "config", "user.name", "Release Test")
    (repo / "setup.py").write_text("pass\n", encoding="utf-8")
    git(repo, "add", "setup.py")
    git(repo, "commit", "-m", "fixture")
    (repo / "setup.py").write_text("changed\n", encoding="utf-8")

    with pytest.raises(ValueError, match="clean worktree"):
        package_release(repo, tmp_path / "CodeGPT.zip", build_id="dirty-build")


def test_release_packaging_rejects_nested_directory_of_another_git_repo(tmp_path):
    parent = tmp_path / "parent"
    package = parent / "codegpt"
    package.mkdir(parents=True)
    git(parent, "init", "-q")
    git(parent, "config", "user.email", "release-test@example.invalid")
    git(parent, "config", "user.name", "Release Test")
    (parent / "unrelated-secret.txt").write_text("must not be archived\n", encoding="utf-8")
    (package / "setup.py").write_text("pass\n", encoding="utf-8")
    git(parent, "add", "unrelated-secret.txt", "codegpt/setup.py")
    git(parent, "commit", "-m", "parent fixture")

    with pytest.raises(ValueError, match="Git top-level"):
        package_release(package, tmp_path / "wrong-tree.zip", build_id="wrong-tree")


def test_manifest_requires_a_valid_utc_timestamp(tmp_path):
    valid = {"product_version": "1.2.0", "source_commit": "a" * 40,
             "build_id": "timestamp-fixture", "built_at_utc": "2026-10-10T00:00:00Z",
             "worktree_dirty": False}
    manifest_path = tmp_path / "build-manifest.json"
    manifest_path.write_text(json.dumps(valid), encoding="utf-8")
    assert read_build_manifest(tmp_path)["manifest_state"] == "valid"

    manifest_path.write_text(json.dumps(valid | {"built_at_utc": "not-a-time"}), encoding="utf-8")
    assert read_build_manifest(tmp_path)["manifest_state"] == "invalid"

    manifest_path.write_text(json.dumps(valid | {"built_at_utc": "2026-10-10T00:00:00+08:00"}), encoding="utf-8")
    assert read_build_manifest(tmp_path)["manifest_state"] == "invalid"


def test_missing_or_invalid_manifest_reports_unknown_build_identity(tmp_path):
    assert read_build_manifest(tmp_path)["source_commit"] is None
    (tmp_path / "build-manifest.json").write_text("{}", encoding="utf-8")
    assert read_build_manifest(tmp_path)["source_commit"] is None
    assert read_build_manifest(tmp_path)["build_id"] is None
