"""Tests for the git_show tool (read a file at a specific git ref)."""

import subprocess

import pytest

from openhands.tools.git_show.definition import GitShowAction, GitShowExecutor


@pytest.fixture
def repo(tmp_path):
    """A small git repo with two branches and a known file."""
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "t@t.dev"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "t"], cwd=root, check=True, capture_output=True
    )
    (root / "a.ts").write_text("line1\nline2\nline3\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "main"], cwd=root, check=True, capture_output=True
    )
    # A feature branch changes the file.
    subprocess.run(
        ["git", "checkout", "-b", "feature"], cwd=root, check=True, capture_output=True
    )
    (root / "a.ts").write_text("line1\nCHANGED\nline3\nline4\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "feature"], cwd=root, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "checkout", "main"], cwd=root, check=True, capture_output=True
    )
    return root


def test_reads_file_at_feature_branch(repo):
    ex = GitShowExecutor(repo)
    obs = ex(GitShowAction(ref="feature", path="a.ts"))
    assert not obs.is_error
    assert "CHANGED" in obs.file_content
    assert obs.total_lines == 4


def test_reads_file_at_main_branch(repo):
    ex = GitShowExecutor(repo)
    obs = ex(GitShowAction(ref="main", path="a.ts"))
    assert not obs.is_error
    assert "CHANGED" not in obs.file_content
    assert obs.total_lines == 3


def test_reads_at_commit_sha(repo):
    sha = subprocess.run(
        ["git", "rev-parse", "feature"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    ex = GitShowExecutor(repo)
    obs = ex(GitShowAction(ref=sha, path="a.ts"))
    assert not obs.is_error
    assert "CHANGED" in obs.file_content


def test_offset_and_limit_page_the_file(repo):
    ex = GitShowExecutor(repo)
    obs = ex(GitShowAction(ref="feature", path="a.ts", offset=1, limit=2))
    assert not obs.is_error
    # offset=1, limit=2 → lines 2-3 (CHANGED, line3)
    assert "CHANGED" in obs.file_content
    assert "line4" not in obs.file_content
    assert obs.is_truncated


def test_bad_ref_reports_error(repo):
    ex = GitShowExecutor(repo)
    obs = ex(GitShowAction(ref="nonexistent-ref", path="a.ts"))
    assert obs.is_error


def test_missing_path_reports_error(repo):
    ex = GitShowExecutor(repo)
    obs = ex(GitShowAction(ref="feature", path="no-such-file.ts"))
    assert obs.is_error
