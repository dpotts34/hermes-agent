"""The ordinary updater must not escape a tested downstream source."""
import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest

from hermes_cli.update_cmd_commit import preflight_refusal

ORIGIN = "https://github.com/example/tested-hermes.git"
POLICY = {
    "schema": 1, "repository": "example/tested-hermes", "branch": "main",
    "upstream_repository": "NousResearch/hermes-agent", "upstream_branch": "main",
    "plugin_repository": "example/optchat", "plugin_commit": "a" * 40,
}
POLICY_FILE = ".optchat-update-policy.json"


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture
def fork(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    for key, value in {
        "HOME": str(home), "HERMES_HOME": str(home / ".hermes"),
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }.items():
        monkeypatch.setenv(key, value)
    source = tmp_path / "fork"
    source.mkdir()
    git(source, "init", "-q", "-b", "main")
    git(source, "remote", "add", "origin", ORIGIN)
    (source / POLICY_FILE).write_text(json.dumps(POLICY) + "\n")
    mark(source)
    return source


def mark(root, *, plugin=None, policy_digest=None, duplicate=False):
    digest = hashlib.sha256((root / POLICY_FILE).read_bytes()).hexdigest()
    message = ("Publish tested candidate\n\nOptChat-Tested-Fork: true\n"
               "OptChat-Upstream-Channel: main\nOptChat-Upstream-SHA: " + "b" * 40 + "\n"
               f"OptChat-Plugin-SHA: {plugin or POLICY['plugin_commit']}\n"
               f"OptChat-Policy-SHA256: {policy_digest or digest}\n")
    if duplicate:
        message += "OptChat-Tested-Fork: true\n"
    git(root, "add", "-A")
    git(root, "commit", "--allow-empty", "-qm", message)
    return git(root, "rev-parse", "HEAD")


def test_native_preflight_accepts_the_pinned_tested_candidate(fork):
    assert preflight_refusal(["git"], fork, "HEAD", ()) is None


@pytest.mark.parametrize("problem", ["unmarked", "wrong_plugin", "wrong_policy_digest", "duplicate_marker", "policy_removed"])
def test_native_preflight_refuses_unverified_or_incompatible_updates_without_moving_head(fork, problem):
    previous = git(fork, "rev-parse", "HEAD")
    if problem == "unmarked":
        git(fork, "commit", "--allow-empty", "-qm", "Raw upstream commit")
    elif problem == "policy_removed":
        git(fork, "rm", POLICY_FILE)
        git(fork, "commit", "-qm", "Remove downstream policy")
    else:
        mark(fork, plugin="c" * 40 if problem == "wrong_plugin" else None,
             policy_digest="0" * 64 if problem == "wrong_policy_digest" else None,
             duplicate=problem == "duplicate_marker")
    target = git(fork, "rev-parse", "HEAD")
    git(fork, "checkout", "--detach", previous)
    refusal = preflight_refusal(["git"], fork, target, ())
    assert refusal is not None and "Tested-fork update refused" in refusal
    assert git(fork, "rev-parse", "HEAD") == previous
    assert git(fork, "status", "--porcelain") == ""


def test_changing_origin_cannot_disable_the_installed_policy(fork):
    git(fork, "remote", "set-url", "origin", "https://github.com/NousResearch/hermes-agent.git")
    refusal = preflight_refusal(["git"], fork, "HEAD", ())
    assert refusal is not None and "origin does not match" in refusal


def test_check_uses_the_tested_fork_even_with_an_official_upstream_remote(fork, tmp_path, monkeypatch, capsys):
    from hermes_cli import update_cmd

    installed = tmp_path / "installed"
    official = tmp_path / "official"
    git(tmp_path, "clone", "-q", str(fork), str(installed))
    git(tmp_path, "clone", "-q", str(fork), str(official))
    git(installed, "remote", "set-url", "origin", ORIGIN)
    git(installed, "config", f"url.{fork}.insteadOf", ORIGIN)
    git(installed, "remote", "add", "upstream", str(official))
    git(official, "rm", POLICY_FILE)
    git(official, "commit", "-qm", "An untested official update")
    fork_target = mark(fork)
    installed_head = git(installed, "rev-parse", "HEAD")
    monkeypatch.setattr(update_cmd._m(), "PROJECT_ROOT", installed)
    update_cmd._cmd_update_check("main", branch_explicit=True)
    assert git(installed, "rev-parse", "origin/main") == fork_target
    assert git(installed, "rev-parse", "HEAD") == installed_head
    assert "Update available" in capsys.readouterr().out
