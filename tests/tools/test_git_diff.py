"""Tests for the git_diff tool (diff between refs / working tree)."""

import subprocess

import pytest

from openhands.tools.git_diff.definition import GitDiffAction, GitDiffExecutor


@pytest.fixture
def repo(tmp_path):
    """A small git repo with two branches and a changed file."""
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
    (root / "a.ts").write_text("line1\nline2\n", encoding="utf-8")
    (root / "b.ts").write_text("orig\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "main"], cwd=root, check=True, capture_output=True
    )
    # Feature branch: modify a.ts, add a file.
    subprocess.run(
        ["git", "checkout", "-b", "feature"], cwd=root, check=True, capture_output=True
    )
    (root / "a.ts").write_text("line1\nCHANGED\n", encoding="utf-8")
    (root / "c.ts").write_text("new\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "feature"], cwd=root, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "checkout", "main"], cwd=root, check=True, capture_output=True
    )
    return root


def test_diff_between_branches(repo):
    ex = GitDiffExecutor(repo)
    obs = ex(GitDiffAction(ref="main...feature"))
    assert not obs.is_error
    assert "+CHANGED" in obs.diff
    assert "c.ts" in obs.diff


def test_diff_single_file(repo):
    ex = GitDiffExecutor(repo)
    obs = ex(GitDiffAction(ref="main...feature", path="a.ts"))
    assert not obs.is_error
    assert "+CHANGED" in obs.diff
    assert "c.ts" not in obs.diff


def test_diff_stat(repo):
    ex = GitDiffExecutor(repo)
    obs = ex(GitDiffAction(ref="main...feature", stat=True))
    assert not obs.is_error
    assert "a.ts" in obs.diff
    assert "c.ts" in obs.diff
    # Stat has no +/- content hunks.
    assert "+CHANGED" not in obs.diff


def test_range_form(repo):
    ex = GitDiffExecutor(repo)
    obs = ex(GitDiffAction(ref_a="main", ref_b="feature"))
    assert not obs.is_error
    assert "+CHANGED" in obs.diff


def test_working_tree_vs_head(repo):
    # Uncommitted change in the working tree.
    (repo / "b.ts").write_text("orig\nedited\n", encoding="utf-8")
    ex = GitDiffExecutor(repo)
    obs = ex(GitDiffAction(ref="HEAD"))
    assert not obs.is_error
    assert "+edited" in obs.diff


def test_bad_ref_reports_error(repo):
    ex = GitDiffExecutor(repo)
    obs = ex(GitDiffAction(ref="nonexistent...main"))
    assert obs.is_error


def test_truncation(repo):
    ex = GitDiffExecutor(repo)
    obs = ex(GitDiffAction(ref="main...feature", max_lines=1))
    assert not obs.is_error
    assert obs.is_truncated
    assert "truncated" in obs.text.lower()
