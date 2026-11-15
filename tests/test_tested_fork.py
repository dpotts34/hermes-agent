"""CLI/Git-remote acceptance tests for safe tested-fork staging and publication."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1]
PIPELINE = SOURCE_ROOT / "scripts" / "tested_fork.py"
CONTROL_REVISION = "test-control-revision-1"


class TestedForkPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="tested-fork-test-")
        self.root = Path(self.temporary.name)
        self.fork_bare = self.root / "fork.git"
        self.upstream_bare = self.root / "upstream.git"
        self.plugin_bare = self.root / "plugin.git"
        self.checker = self.root / "checker.py"
        self.checker_log = self.root / "checker-runs.log"
        self._git("init", "--bare", "--initial-branch=main", str(self.fork_bare))
        self._git("init", "--bare", "--initial-branch=main", str(self.upstream_bare))
        self._git("init", "--bare", "--initial-branch=main", str(self.plugin_bare))
        self.plugin_sha = self._make_plugin()
        self.old_main_sha, self.seed_sha = self._make_fork_and_seed()
        self.upstream_sha = self._make_upstream()
        self._write_checker()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _git(*args: str, cwd: Path | None = None) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if result.returncode:
            raise AssertionError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
        return result.stdout.strip()

    def _new_repo(self, name: str) -> Path:
        repository = self.root / name
        self._git("init", "--initial-branch=main", str(repository))
        self._git("config", "user.name", "Integration Test", cwd=repository)
        self._git("config", "user.email", "integration-test@example.invalid", cwd=repository)
        return repository

    def _commit(self, repository: Path, message: str) -> str:
        self._git("add", "-A", cwd=repository)
        self._git("commit", "-m", message, cwd=repository)
        return self._git("rev-parse", "HEAD", cwd=repository)

    def _make_plugin(self) -> str:
        repository = self._new_repo("plugin-work")
        (repository / "plugin.py").write_text("PINNED_PLUGIN = True\n", encoding="utf-8")
        plugin_sha = self._commit(repository, "plugin: create synthetic pinned plugin")
        self._git("remote", "add", "origin", str(self.plugin_bare), cwd=repository)
        self._git("push", "origin", "main", cwd=repository)
        return plugin_sha

    def _make_fork_and_seed(self) -> tuple[str, str]:
        repository = self._new_repo("fork-work")
        (repository / "core.py").write_text("base = True\n", encoding="utf-8")
        old_main_sha = self._commit(repository, "core: initial published main")
        self._git("remote", "add", "origin", str(self.fork_bare), cwd=repository)
        self._git("push", "origin", "main", cwd=repository)

        self._git("checkout", "-b", "optchat/seed", cwd=repository)
        (repository / "repairs.txt").write_text("tested fork repair\n", encoding="utf-8")
        policy = {
            "schema": 1,
            "repository": "dpotts34/hermes-agent",
            "branch": "main",
            "upstream_repository": "NousResearch/hermes-agent",
            "upstream_branch": "main",
            "plugin_repository": "dpotts34/Hermes-OptChat-Plugin",
            "plugin_commit": self.plugin_sha,
        }
        (repository / ".optchat-update-policy.json").write_text(
            json.dumps(policy, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        seed_sha = self._commit(repository, "core: prepare synthetic tested seed")
        self._git("push", "origin", "optchat/seed", cwd=repository)
        return old_main_sha, seed_sha

    def _make_upstream(self) -> str:
        repository = self.root / "upstream-work"
        self._git("clone", str(self.fork_bare), str(repository))
        self._git("config", "user.name", "Official Upstream", cwd=repository)
        self._git("config", "user.email", "upstream@example.invalid", cwd=repository)
        (repository / "upstream.txt").write_text("official main update\n", encoding="utf-8")
        upstream_sha = self._commit(repository, "upstream: add a non-conflicting update")
        self._git("remote", "add", "official", str(self.upstream_bare), cwd=repository)
        self._git("push", "official", "HEAD:main", cwd=repository)
        return upstream_sha

    def _write_checker(self) -> None:
        self.checker.write_text(
            "\n".join(
                [
                    "import argparse",
                    "import os",
                    "from pathlib import Path",
                    "import subprocess",
                    "import sys",
                    "parser = argparse.ArgumentParser()",
                    "parser.add_argument('--core', required=True)",
                    "parser.add_argument('--plugin', required=True)",
                    "parser.add_argument('--artifacts', required=True)",
                    "args = parser.parse_args()",
                    "core = Path(args.core)",
                    "plugin = Path(args.plugin)",
                    "artifacts = Path(args.artifacts)",
                    "artifacts.mkdir()",
                    "if not (core / 'repairs.txt').is_file(): sys.exit(21)",
                    "if not (core / 'upstream.txt').is_file(): sys.exit(22)",
                    "if not (plugin / 'plugin.py').is_file(): sys.exit(23)",
                    "with open(os.environ['CHECKER_RUN_LOG'], 'a', encoding='utf-8') as log: log.write('checked\\n')",
                    "(artifacts / 'checked-candidate.txt').write_text(subprocess.check_output(['git', '-C', str(core), 'rev-parse', 'HEAD'], text=True), encoding='utf-8')",
                    "sys.exit(int(os.environ.get('CHECKER_EXIT_CODE', '0'))) ",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

    def _run(self, command: str, *extra: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        child_env = os.environ.copy()
        child_env["OPTCHAT_CONTROL_REVISION"] = CONTROL_REVISION
        child_env["CHECKER_RUN_LOG"] = str(self.checker_log)
        if env:
            child_env.update(env)
        return subprocess.run(
            [sys.executable, str(PIPELINE), command, *extra],
            cwd=SOURCE_ROOT,
            env=child_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def _stage(self, name: str, *, env: dict[str, str] | None = None) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
        work = self.root / f"{name}-work"
        artifacts = self.root / f"{name}-artifacts"
        result = self._run(
            "stage",
            "--work-dir",
            str(work),
            "--artifacts",
            str(artifacts),
            "--checker",
            str(self.checker),
            "--fork-url",
            str(self.fork_bare),
            "--upstream-url",
            str(self.upstream_bare),
            "--plugin-url",
            str(self.plugin_bare),
            "--plugin-sha",
            self.plugin_sha,
            env=env,
        )
        return result, work, artifacts

    def _publish(self, name: str, artifacts: Path) -> subprocess.CompletedProcess[str]:
        return self._run(
            "publish",
            "--artifacts",
            str(artifacts),
            "--work-dir",
            str(self.root / f"{name}-publish-work"),
            "--checker",
            str(self.checker),
            "--fork-url",
            str(self.fork_bare),
            "--plugin-sha",
            self.plugin_sha,
        )

    def _remote_main(self) -> str:
        return self._git("rev-parse", "refs/heads/main", cwd=self.fork_bare)

    def _add_remote_racer_commit(self) -> str:
        repository = self.root / "racer-work"
        self._git("clone", str(self.fork_bare), str(repository))
        self._git("config", "user.name", "Race Writer", cwd=repository)
        self._git("config", "user.email", "race@example.invalid", cwd=repository)
        (repository / "racing-change.txt").write_text("arrived after staging\n", encoding="utf-8")
        racer_sha = self._commit(repository, "core: race staged promotion")
        self._git("push", "origin", "main", cwd=repository)
        return racer_sha

    def test_checker_failure_never_changes_published_main(self) -> None:
        result, _, artifacts = self._stage("checker-failure", env={"CHECKER_EXIT_CODE": "17"})

        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self._remote_main(), self.old_main_sha)
        self.assertFalse((artifacts / "manifest.json").exists())
        self.assertFalse((artifacts / "candidate.bundle").exists())
        self.assertEqual(self.checker_log.read_text(encoding="utf-8"), "checked\n")

    def test_merge_conflict_never_changes_published_main(self) -> None:
        upstream_work = self.root / "upstream-work"
        (upstream_work / "repairs.txt").write_text("conflicting official change\n", encoding="utf-8")
        conflict_sha = self._commit(upstream_work, "upstream: conflict with tested repair")
        self._git("push", "official", "HEAD:main", cwd=upstream_work)

        result, _, artifacts = self._stage("merge-conflict")

        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self._remote_main(), self.old_main_sha)
        self.assertFalse((artifacts / "manifest.json").exists())
        self.assertFalse(self.checker_log.exists())
        self.assertNotEqual(conflict_sha, self.upstream_sha)

    def test_remote_race_after_checks_rejects_bundle_without_rewinding_main(self) -> None:
        stage_result, _, artifacts = self._stage("publication-race")
        self.assertEqual(stage_result.returncode, 0, stage_result.stdout + stage_result.stderr)
        candidate_sha = json.loads((artifacts / "manifest.json").read_text(encoding="utf-8"))["candidate_sha"]
        racer_sha = self._add_remote_racer_commit()

        publish_result = self._publish("publication-race", artifacts)

        self.assertNotEqual(publish_result.returncode, 0, publish_result.stdout + publish_result.stderr)
        self.assertEqual(self._remote_main(), racer_sha)
        self.assertNotEqual(self._remote_main(), candidate_sha)

    def test_success_promotes_only_checked_history_and_skips_identical_followup(self) -> None:
        stage_result, _, artifacts = self._stage("successful-promotion")
        self.assertEqual(stage_result.returncode, 0, stage_result.stdout + stage_result.stderr)
        manifest = json.loads((artifacts / "manifest.json").read_text(encoding="utf-8"))
        candidate_sha = manifest["candidate_sha"]
        self.assertEqual(manifest["status"], "promotion")
        self.assertEqual(manifest["old_main_sha"], self.old_main_sha)
        self.assertEqual(manifest["upstream_sha"], self.upstream_sha)
        self.assertEqual(manifest["plugin_sha"], self.plugin_sha)
        self.assertEqual(self._remote_main(), self.old_main_sha)
        self.assertEqual((artifacts / "checks" / "checked-candidate.txt").read_text(encoding="utf-8").strip(), candidate_sha)

        publish_result = self._publish("successful-promotion", artifacts)
        self.assertEqual(publish_result.returncode, 0, publish_result.stdout + publish_result.stderr)
        self.assertEqual(self._remote_main(), candidate_sha)
        self._git("merge-base", "--is-ancestor", self.old_main_sha, candidate_sha, cwd=self.fork_bare)
        self._git("merge-base", "--is-ancestor", self.seed_sha, candidate_sha, cwd=self.fork_bare)
        self._git("merge-base", "--is-ancestor", self.upstream_sha, candidate_sha, cwd=self.fork_bare)
        remote_refs = self._git("for-each-ref", "--format=%(refname)", "refs/heads", cwd=self.fork_bare).splitlines()
        self.assertEqual(set(remote_refs), {"refs/heads/main", "refs/heads/optchat/seed"})

        no_update, _, no_update_artifacts = self._stage("already-tested")
        self.assertEqual(no_update.returncode, 0, no_update.stdout + no_update.stderr)
        no_update_manifest = json.loads((no_update_artifacts / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(no_update_manifest["status"], "no_update")
        self.assertEqual(no_update_manifest["candidate_sha"], candidate_sha)
        self.assertFalse((no_update_artifacts / "candidate.bundle").exists())
        self.assertEqual(self.checker_log.read_text(encoding="utf-8"), "checked\n")


if __name__ == "__main__":
    unittest.main()
