#!/usr/bin/env python3
"""Stage, test, and compare-and-swap publish a tested fork/main candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

FORK_REPOSITORY = "dpotts34/hermes-agent"
FORK_URL = f"https://github.com/{FORK_REPOSITORY}.git"
UPSTREAM_REPOSITORY = "NousResearch/hermes-agent"
UPSTREAM_URL = f"https://github.com/{UPSTREAM_REPOSITORY}.git"
PLUGIN_REPOSITORY = "dpotts34/Hermes-OptChat-Plugin"
PLUGIN_URL = f"https://github.com/{PLUGIN_REPOSITORY}.git"
PINNED_PLUGIN_COMMIT = "b41dee60c4328137cff18c34cb7cab0b352e9e1a"
RUNTIME_BRANCH = "main"
SEED_BRANCH = "optchat/seed"
POLICY_PATH = ".optchat-update-policy.json"
MANIFEST_NAME = "manifest.json"
STATUS_NAME = "status.json"
BUNDLE_NAME = "candidate.bundle"
CANDIDATE_REF = "refs/heads/optchat-tested-candidate"
CHECKER_VERSION = "1"
HEX_SHA1 = re.compile(r"^[0-9a-f]{40}$")
HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class PipelineError(RuntimeError):
    """A fail-closed staging or publishing error."""


def git(repository: Path | None, *args: str, check: bool = True) -> str:
    command = ["git"]
    if repository is not None:
        command += ["-C", str(repository)]
    command.extend(args)
    result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise PipelineError(f"git {' '.join(args)} failed ({result.returncode}): {detail}")
    return result.stdout.strip()


def is_ancestor(repository: Path, ancestor: str, descendant: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(repository), "merge-base", "--is-ancestor", ancestor, descendant],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode not in (0, 1):
        raise PipelineError(f"git merge-base failed: {result.stderr.strip()}")
    return result.returncode == 0


def resolve_commit(repository: Path, ref: str) -> str:
    commit = git(repository, "rev-parse", "--verify", f"{ref}^{{commit}}")
    if not HEX_SHA1.fullmatch(commit):
        raise PipelineError(f"Expected a full SHA-1 commit ID for {ref}, got {commit!r}")
    return commit


def has_policy(repository: Path, commit: str) -> bool:
    return subprocess.run(
        ["git", "-C", str(repository), "cat-file", "-e", f"{commit}:{POLICY_PATH}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0


def commit_trailers(repository: Path, commit: str) -> dict[str, str]:
    message = git(repository, "show", "-s", "--format=%B", commit)
    parsed: dict[str, str] = {}
    for line in message.splitlines():
        key, separator, value = line.partition(":")
        if separator and key.startswith("OptChat-"):
            parsed[key] = value.strip()
    return parsed


def has_tested_marker(repository: Path, commit: str) -> bool:
    return bool(
        git(
            repository,
            "log",
            "--format=%H",
            "--grep=^OptChat-Tested-Fork: true$",
            commit,
        )
    )


def policy_bytes(repository: Path, commit: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repository), "show", f"{commit}:{POLICY_PATH}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode:
        raise PipelineError(f"Commit {commit} does not contain {POLICY_PATH}: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout


def validate_policy(
    raw: bytes,
    *,
    fork_repository: str,
    plugin_repository: str,
    plugin_sha: str,
    upstream_repository: str,
    upstream_branch: str,
) -> str:
    try:
        policy = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PipelineError(f"{POLICY_PATH} is not valid JSON: {exc}") from exc
    if not isinstance(policy, dict):
        raise PipelineError(f"{POLICY_PATH} must contain a JSON object")
    expected: dict[str, Any] = {
        "schema": 1,
        "repository": fork_repository,
        "branch": RUNTIME_BRANCH,
        "upstream_repository": upstream_repository,
        "upstream_branch": upstream_branch,
        "plugin_repository": plugin_repository,
        "plugin_commit": plugin_sha,
    }
    if set(policy) != set(expected):
        raise PipelineError(f"{POLICY_PATH} keys do not match the supported schema")
    for key, value in expected.items():
        if policy.get(key) != value or (key == "schema" and type(policy.get(key)) is not int):
            raise PipelineError(f"{POLICY_PATH} has unexpected {key!r}; expected {value!r}")
    return hashlib.sha256(raw).hexdigest()


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")

def get_control_revision() -> str:
    revision = (
        os.environ.get("OPTCHAT_CONTROL_REVISION")
        or os.environ.get("GITHUB_SHA")
        or "local-" + hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    )
    if not revision or any(character.isspace() for character in revision):
        raise PipelineError("Control revision must be a non-empty token without whitespace")
    return revision

def set_workflow_outputs(*, publish: bool, candidate_sha: str, status: str) -> None:
    output_file = os.environ.get("GITHUB_OUTPUT")
    if not output_file:
        return
    with open(output_file, "a", encoding="utf-8") as output:
        output.write(f"publish={'true' if publish else 'false'}\n")
        output.write(f"candidate_sha={candidate_sha}\n")
        output.write(f"status={status}\n")


def initialize_remote(repository: Path, name: str, url: str) -> None:
    git(repository, "remote", "add", name, url)


def fetch_ref(repository: Path, remote: str, branch: str, local_ref: str) -> str:
    git(
        repository,
        "fetch",
        "--quiet",
        "--no-tags",
        remote,
        f"+refs/heads/{branch}:{local_ref}",
    )
    return resolve_commit(repository, local_ref)


def fetch_plugin(plugin_path: Path, plugin_url: str, plugin_sha: str) -> str:
    git(None, "init", "--quiet", str(plugin_path))
    initialize_remote(plugin_path, "plugin", plugin_url)
    git(plugin_path, "fetch", "--quiet", "--depth=1", "--no-tags", "plugin", plugin_sha)
    git(plugin_path, "checkout", "--quiet", "--detach", "FETCH_HEAD")
    actual = resolve_commit(plugin_path, "HEAD")
    if actual != plugin_sha:
        raise PipelineError(f"Plugin checkout mismatch: wanted {plugin_sha}, got {actual}")
    return actual


def manifest_base(
    *,
    status: str,
    candidate_sha: str,
    old_main_sha: str,
    upstream_sha: str,
    policy_sha256: str,
    fork_repository: str,
    upstream_repository: str,
    upstream_branch: str,
    plugin_repository: str,
    plugin_sha: str,
    checker_sha256: str,
    control_revision: str,
) -> dict[str, Any]:
    return {
        "schema": 1,
        "status": status,
        "repository": fork_repository,
        "runtime_branch": RUNTIME_BRANCH,
        "old_main_sha": old_main_sha,
        "candidate_sha": candidate_sha,
        "candidate_ref": CANDIDATE_REF,
        "upstream_repository": upstream_repository,
        "upstream_channel": "main",
        "upstream_branch": upstream_branch,
        "upstream_tag": None,
        "upstream_sha": upstream_sha,
        "plugin_repository": plugin_repository,
        "plugin_sha": plugin_sha,
        "policy_file": POLICY_PATH,
        "policy_sha256": policy_sha256,
        "checker_version": CHECKER_VERSION,
        "checker_sha256": checker_sha256,
        "control_revision": control_revision,
    }


def recorded_candidate_is_current(
    repository: Path,
    head_sha: str,
    *,
    upstream_sha: str,
    plugin_sha: str,
    policy_sha256: str,
    checker_sha256: str,
    control_revision: str,
) -> bool:
    required = {
        "OptChat-Tested-Fork": "true",
        "OptChat-Upstream-Channel": "main",
        "OptChat-Upstream-Branch": "main",
        "OptChat-Upstream-Tag": "null",
        "OptChat-Upstream-SHA": upstream_sha,
        "OptChat-Plugin-SHA": plugin_sha,
        "OptChat-Policy-SHA256": policy_sha256,
        "OptChat-Checker-Version": CHECKER_VERSION,
        "OptChat-Checker-SHA256": checker_sha256,
        "OptChat-Control-Revision": control_revision,
    }
    trailers = commit_trailers(repository, head_sha)
    return (
        all(trailers.get(key) == value for key, value in required.items())
        and re.fullmatch(r"\d+\.\d+\.\d+", trailers.get("OptChat-Python-Version", "")) is not None
    )


def prepare_candidate_python(core: Path, work_dir: Path, install_dependencies: bool) -> tuple[Path, str]:
    if install_dependencies:
        isolated_home = work_dir / "home"
        hermes_home = work_dir / "hermes-home"
        runtime_dir = work_dir / "runtime"
        cache_dir = work_dir / "cache"
        isolated_paths = {
            "HOME": isolated_home,
            "HERMES_HOME": hermes_home,
            "HERMES_RUNTIME_DIR": runtime_dir,
            "XDG_CACHE_HOME": cache_dir,
            "XDG_CONFIG_HOME": work_dir / "xdg-config",
            "XDG_DATA_HOME": work_dir / "xdg-data",
            "TMPDIR": work_dir / "tmp",
            "TMP": work_dir / "tmp",
            "TEMP": work_dir / "tmp",
        }
        for path in isolated_paths.values():
            path.mkdir(parents=True, exist_ok=True)
        os.environ.update({key: str(path) for key, path in isolated_paths.items()})
        os.environ["UV_CACHE_DIR"] = str(cache_dir / "uv")
        environment = os.environ.copy()
        check_lock = subprocess.run(
            [sys.executable, "-m", "pm.build_env", "--source", str(core), "--check-lock"],
            cwd=core,
            env=environment,
        )
        if check_lock.returncode:
            raise PipelineError(f"Candidate dependency lock check failed with exit code {check_lock.returncode}")
        candidate_environment = work_dir / "candidate-venv"
        build = subprocess.run(
            [
                sys.executable,
                "-m",
                "pm.build_env",
                "--source",
                str(core),
                "--out",
                str(candidate_environment),
                "--group",
                "dev",
                "--group",
                "test",
            ],
            cwd=core,
            env=environment,
        )
        if build.returncode:
            raise PipelineError(f"Candidate dev/test environment build failed with exit code {build.returncode}")
        python = candidate_environment / "bin" / "python"
    else:
        python = Path(sys.executable)
    if not python.is_file():
        raise PipelineError(f"Candidate test environment did not create its interpreter: {python}")
    version_result = subprocess.run(
        [str(python), "-c", "import sys; print(sys.version.split()[0])"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if version_result.returncode:
        raise PipelineError(f"Cannot query checker Python version: {version_result.stderr.strip()}")
    version = version_result.stdout.strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise PipelineError(f"Checker Python returned an invalid version: {version!r}")
    return python, version


def run_checker(
    checker: Path,
    core: Path,
    plugin: Path,
    artifacts: Path,
    *,
    python: Path,
    candidate_sha: str,
    plugin_sha: str,
) -> None:
    if not checker.is_file():
        raise PipelineError(f"Candidate checker not found: {checker}")
    checker_artifacts = artifacts / "checks"
    command = [
        str(python),
        str(checker),
        "--core",
        str(core),
        "--plugin",
        str(plugin),
        "--artifacts",
        str(checker_artifacts),
    ]
    environment = os.environ.copy()
    environment["HERMES_PYTHON"] = str(python)
    result = subprocess.run(command, env=environment)
    if result.returncode:
        raise PipelineError(f"Candidate checker failed with exit code {result.returncode}")
    current_head = resolve_commit(core, "HEAD")
    if current_head != candidate_sha or resolve_commit(core, CANDIDATE_REF) != candidate_sha:
        raise PipelineError("Candidate checker changed the staged candidate SHA or ref")
    for repository, expected, label in ((core, candidate_sha, "candidate"), (plugin, plugin_sha, "plugin")):
        if resolve_commit(repository, "HEAD") != expected:
            raise PipelineError(f"Candidate checker changed the pinned {label} checkout")
        diff = subprocess.run(
            ["git", "-C", str(repository), "diff", "--quiet", "HEAD", "--"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        if diff.returncode:
            raise PipelineError(f"Candidate checker modified tracked {label} files")


def stage(args: argparse.Namespace) -> int:
    checker = args.checker.resolve()
    if not checker.is_file():
        raise PipelineError(f"Candidate checker not found: {checker}")
    checker_sha256 = hashlib.sha256(checker.read_bytes()).hexdigest()
    control_revision = get_control_revision()
    work_dir: Path = args.work_dir.resolve()
    artifacts: Path = args.artifacts.resolve()
    if work_dir == artifacts or work_dir in artifacts.parents or artifacts in work_dir.parents:
        raise PipelineError("Work and artifact directories must be separate and non-overlapping")
    work_dir.mkdir(parents=True, exist_ok=False)
    artifacts.mkdir(parents=True, exist_ok=False)

    core = work_dir / "core"
    plugin = work_dir / "plugin"
    git(None, "init", "--quiet", str(core))
    initialize_remote(core, "origin", args.fork_url)
    old_main_sha = fetch_ref(core, "origin", RUNTIME_BRANCH, "refs/remotes/origin/main")
    initialize_remote(core, "official", args.upstream_url)
    upstream_sha = fetch_ref(core, "official", args.upstream_branch, "refs/remotes/official/main")
    plugin_sha = fetch_plugin(plugin, args.plugin_url, args.plugin_sha)

    main_has_marker = has_tested_marker(core, old_main_sha)
    main_has_policy = has_policy(core, old_main_sha)
    bootstrap = not main_has_marker and not main_has_policy
    if bootstrap:
        seed_sha = fetch_ref(core, "origin", SEED_BRANCH, "refs/remotes/origin/optchat/seed")
        if not is_ancestor(core, old_main_sha, seed_sha):
            raise PipelineError(
                f"Bootstrap seed {seed_sha} does not descend from published main {old_main_sha}; refusing to replace history"
            )
        source_sha = seed_sha
    else:
        source_sha = old_main_sha

    raw_policy = policy_bytes(core, source_sha)
    policy_sha256 = validate_policy(
        raw_policy,
        fork_repository=args.fork_repository,
        plugin_repository=args.plugin_repository,
        plugin_sha=plugin_sha,
        upstream_repository=args.upstream_repository,
        upstream_branch=args.upstream_branch,
    )

    if (
        not bootstrap
        and is_ancestor(core, upstream_sha, old_main_sha)
        and recorded_candidate_is_current(
            core,
            old_main_sha,
            upstream_sha=upstream_sha,
            plugin_sha=plugin_sha,
            policy_sha256=policy_sha256,
            checker_sha256=checker_sha256,
            control_revision=control_revision,
        )
    ):
        manifest = manifest_base(
            status="no_update",
            candidate_sha=old_main_sha,
            old_main_sha=old_main_sha,
            upstream_sha=upstream_sha,
            policy_sha256=policy_sha256,
            fork_repository=args.fork_repository,
            upstream_repository=args.upstream_repository,
            upstream_branch=args.upstream_branch,
            plugin_repository=args.plugin_repository,
            plugin_sha=plugin_sha,
            checker_sha256=checker_sha256,
            control_revision=control_revision,
        )
        manifest["checker_status"] = "previously_passed"
        manifest["python_version"] = commit_trailers(core, old_main_sha)["OptChat-Python-Version"]
        write_json(artifacts / MANIFEST_NAME, manifest)
        write_json(artifacts / STATUS_NAME, {"status": "no_update", "publish": False, "candidate_sha": old_main_sha})
        set_workflow_outputs(publish=False, candidate_sha=old_main_sha, status="no_update")
        print(f"No promotion needed: main {old_main_sha} already records upstream {upstream_sha} and the pinned policy/plugin.")
        return 0

    git(core, "checkout", "--quiet", "--detach", source_sha)
    if not is_ancestor(core, upstream_sha, source_sha):
        # A regular no-ff merge preserves both histories. A conflict aborts staging;
        # the published branch is never touched by this job.
        git(core, "-c", "user.name=OptChat Maintenance Bot", "-c", "user.email=optchat-maintenance@users.noreply.github.com", "merge", "--no-edit", "--no-ff", upstream_sha)

    policy_sha256 = validate_policy(
        policy_bytes(core, resolve_commit(core, "HEAD")),
        fork_repository=args.fork_repository,
        plugin_repository=args.plugin_repository,
        plugin_sha=plugin_sha,
        upstream_repository=args.upstream_repository,
        upstream_branch=args.upstream_branch,
    )
    checker_python, python_version = prepare_candidate_python(
        core,
        work_dir,
        args.install_candidate_dependencies,
    )
    git(
        core,
        "update-ref",
        CANDIDATE_REF,
        resolve_commit(core, "HEAD"),
    )
    trailers = [
        "OptChat-Tested-Fork: true",
        "OptChat-Old-Main-SHA: " + old_main_sha,
        "OptChat-Upstream-Channel: main",
        "OptChat-Upstream-Branch: " + args.upstream_branch,
        "OptChat-Upstream-Tag: null",
        "OptChat-Python-Version: " + python_version,
        "OptChat-Upstream-SHA: " + upstream_sha,
        "OptChat-Plugin-SHA: " + plugin_sha,
        "OptChat-Policy-SHA256: " + policy_sha256,
        "OptChat-Checker-Version: " + CHECKER_VERSION,
        "OptChat-Checker-SHA256: " + checker_sha256,
        "OptChat-Control-Revision: " + control_revision,
    ]
    git(
        core,
        "-c",
        "user.name=OptChat Maintenance Bot",
        "-c",
        "user.email=optchat-maintenance@users.noreply.github.com",
        "commit",
        "--allow-empty",
        "-m",
        "chore: record tested-fork promotion candidate",
        "-m",
        "\n".join(trailers),
    )
    candidate_sha = resolve_commit(core, "HEAD")
    git(core, "update-ref", CANDIDATE_REF, candidate_sha)
    if not is_ancestor(core, old_main_sha, candidate_sha):
        raise PipelineError("Staged candidate does not descend from the published main captured at stage time")
    if not is_ancestor(core, upstream_sha, candidate_sha):
        raise PipelineError("Staged candidate does not contain the selected official upstream commit")

    run_checker(
        checker,
        core,
        plugin,
        artifacts,
        python=checker_python,
        candidate_sha=candidate_sha,
        plugin_sha=plugin_sha,
    )
    manifest = manifest_base(
        status="promotion",
        candidate_sha=candidate_sha,
        old_main_sha=old_main_sha,
        upstream_sha=upstream_sha,
        policy_sha256=policy_sha256,
        fork_repository=args.fork_repository,
        upstream_repository=args.upstream_repository,
        upstream_branch=args.upstream_branch,
        plugin_repository=args.plugin_repository,
        plugin_sha=plugin_sha,
        checker_sha256=checker_sha256,
        control_revision=control_revision,
    )
    manifest["checker_status"] = "passed"
    manifest["python_version"] = python_version
    bundle_path = artifacts / BUNDLE_NAME
    git(core, "bundle", "create", str(bundle_path), CANDIDATE_REF, "^" + old_main_sha)
    manifest["bundle"] = BUNDLE_NAME
    manifest["bundle_sha256"] = hashlib.sha256(bundle_path.read_bytes()).hexdigest()
    write_json(artifacts / MANIFEST_NAME, manifest)
    write_json(artifacts / STATUS_NAME, {"status": "promotion", "publish": True, "candidate_sha": candidate_sha})
    set_workflow_outputs(publish=True, candidate_sha=candidate_sha, status="promotion")
    print(f"Candidate {candidate_sha} passed the checker; bundle and evidence are ready for compare-and-swap publication.")
    return 0


def read_manifest(
    artifact_dir: Path,
    plugin_sha: str,
    checker_sha256: str,
    control_revision: str,
) -> tuple[dict[str, Any], Path]:
    manifest_path = artifact_dir / MANIFEST_NAME
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineError(f"Cannot read {manifest_path}: {exc}") from exc
    if not isinstance(manifest, dict):
        raise PipelineError("Promotion manifest must be a JSON object")
    required = {
        "schema": 1,
        "status": "promotion",
        "repository": FORK_REPOSITORY,
        "runtime_branch": RUNTIME_BRANCH,
        "candidate_ref": CANDIDATE_REF,
        "upstream_repository": UPSTREAM_REPOSITORY,
        "upstream_channel": "main",
        "upstream_branch": "main",
        "upstream_tag": None,
        "plugin_repository": PLUGIN_REPOSITORY,
        "plugin_sha": plugin_sha,
        "policy_file": POLICY_PATH,
        "checker_version": CHECKER_VERSION,
        "checker_sha256": checker_sha256,
        "control_revision": control_revision,
        "checker_status": "passed",
        "bundle": BUNDLE_NAME,
    }
    for key, value in required.items():
        if manifest.get(key) != value or (key == "schema" and type(manifest.get(key)) is not int):
            raise PipelineError(f"Promotion manifest has unexpected {key!r}; expected {value!r}")
    for key in ("old_main_sha", "candidate_sha", "upstream_sha"):
        if not isinstance(manifest.get(key), str) or not HEX_SHA1.fullmatch(manifest[key]):
            raise PipelineError(f"Promotion manifest has invalid {key}")
    if not isinstance(manifest.get("policy_sha256"), str) or not HEX_SHA256.fullmatch(manifest["policy_sha256"]):
        raise PipelineError("Promotion manifest has invalid policy_sha256")
    if not isinstance(manifest.get("python_version"), str) or not re.fullmatch(r"\d+\.\d+\.\d+", manifest["python_version"]):
        raise PipelineError("Promotion manifest has invalid python_version")
    if not isinstance(manifest.get("bundle_sha256"), str) or not HEX_SHA256.fullmatch(manifest["bundle_sha256"]):
        raise PipelineError("Promotion manifest has invalid bundle_sha256")
    status_path = artifact_dir / STATUS_NAME
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineError(f"Cannot read {status_path}: {exc}") from exc
    if status != {
        "status": "promotion",
        "publish": True,
        "candidate_sha": manifest["candidate_sha"],
    }:
        raise PipelineError("Promotion status does not agree with the candidate manifest")
    bundle_path = artifact_dir / BUNDLE_NAME
    try:
        digest = hashlib.sha256(bundle_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise PipelineError(f"Cannot read promotion bundle: {exc}") from exc
    if digest != manifest["bundle_sha256"]:
        raise PipelineError("Promotion bundle checksum does not match the manifest")
    return manifest, bundle_path


def publish(args: argparse.Namespace) -> int:
    checker = args.checker.resolve()
    if not checker.is_file():
        raise PipelineError(f"Candidate checker not found: {checker}")
    checker_sha256 = hashlib.sha256(checker.read_bytes()).hexdigest()
    control_revision = get_control_revision()
    artifact_dir: Path = args.artifacts.resolve()
    manifest, bundle_path = read_manifest(
        artifact_dir,
        args.plugin_sha,
        checker_sha256,
        control_revision,
    )
    repository = args.work_dir.resolve()
    if repository == artifact_dir or repository in artifact_dir.parents or artifact_dir in repository.parents:
        raise PipelineError("Publisher work and artifact directories must be separate and non-overlapping")
    repository.mkdir(parents=True, exist_ok=False)
    git(None, "init", "--bare", "--quiet", str(repository))
    initialize_remote(repository, "fork", args.fork_url)
    actual_main = fetch_ref(repository, "fork", RUNTIME_BRANCH, "refs/remotes/fork/main")
    expected_old_main = manifest["old_main_sha"]
    if actual_main != expected_old_main:
        raise PipelineError(
            f"Published main raced after staging: expected {expected_old_main}, found {actual_main}; refusing publication"
        )

    git(repository, "bundle", "verify", str(bundle_path), check=True)
    listed = git(None, "bundle", "list-heads", str(bundle_path)).splitlines()
    refs = {}
    for line in listed:
        fields = line.split()
        if len(fields) != 2:
            raise PipelineError("Promotion bundle has an invalid advertised ref")
        commit, ref = fields
        if not HEX_SHA1.fullmatch(commit):
            raise PipelineError("Promotion bundle has an invalid advertised commit")
        refs[ref] = commit
    if refs != {CANDIDATE_REF: manifest["candidate_sha"]}:
        raise PipelineError("Promotion bundle does not contain exactly the manifest candidate ref")
    git(repository, "fetch", "--quiet", str(bundle_path), f"{CANDIDATE_REF}:refs/heads/candidate")
    candidate_sha = resolve_commit(repository, "refs/heads/candidate")
    if candidate_sha != manifest["candidate_sha"]:
        raise PipelineError("Fetched bundle candidate does not match manifest SHA")
    if not is_ancestor(repository, expected_old_main, candidate_sha):
        raise PipelineError("Candidate is not a forward descendant of the captured main")
    if not is_ancestor(repository, manifest["upstream_sha"], candidate_sha):
        raise PipelineError("Candidate does not contain the manifest upstream commit")

    trailers = commit_trailers(repository, candidate_sha)
    expected_trailers = {
        "OptChat-Tested-Fork": "true",
        "OptChat-Old-Main-SHA": expected_old_main,
        "OptChat-Upstream-Channel": "main",
        "OptChat-Upstream-Branch": "main",
        "OptChat-Upstream-Tag": "null",
        "OptChat-Upstream-SHA": manifest["upstream_sha"],
        "OptChat-Plugin-SHA": args.plugin_sha,
        "OptChat-Policy-SHA256": manifest["policy_sha256"],
        "OptChat-Checker-Version": CHECKER_VERSION,
        "OptChat-Checker-SHA256": manifest["checker_sha256"],
        "OptChat-Control-Revision": manifest["control_revision"],
        "OptChat-Python-Version": manifest["python_version"],
    }
    if any(trailers.get(key) != value for key, value in expected_trailers.items()):
        raise PipelineError("Candidate commit trailers do not match the tested manifest")
    raw_policy = policy_bytes(repository, candidate_sha)
    actual_policy_sha = validate_policy(
        raw_policy,
        fork_repository=FORK_REPOSITORY,
        plugin_repository=PLUGIN_REPOSITORY,
        plugin_sha=args.plugin_sha,
        upstream_repository=UPSTREAM_REPOSITORY,
        upstream_branch=RUNTIME_BRANCH,
    )
    if actual_policy_sha != manifest["policy_sha256"]:
        raise PipelineError("Candidate update policy checksum does not match the manifest")

    # The lease is the compare-and-swap: we first prove candidate descends from
    # old_main, and the remote must still equal that exact SHA at receive time.
    # Therefore the accepted update is strictly fast-forward; no rebase or
    # non-fast-forward update is possible.
    token = os.environ.get("GITHUB_TOKEN", "")
    push_env = os.environ.copy()
    if args.fork_url.startswith("https://github.com/"):
        if not token:
            raise PipelineError("GITHUB_TOKEN is required for GitHub publication")
        import base64

        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
        push_env["GIT_CONFIG_COUNT"] = "1"
        push_env["GIT_CONFIG_KEY_0"] = "http.https://github.com/.extraheader"
        push_env["GIT_CONFIG_VALUE_0"] = f"AUTHORIZATION: basic {basic}"

    command = [
        "git",
        "-C",
        str(repository),
        "push",
        "--porcelain",
        f"--force-with-lease=refs/heads/main:{expected_old_main}",
        "fork",
        "refs/heads/candidate:refs/heads/main",
    ]
    pushed = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=push_env)
    if pushed.returncode:
        detail = pushed.stderr.strip() or pushed.stdout.strip()
        raise PipelineError(f"Compare-and-swap publication failed; main was not updated: {detail}")
    published_main = git(None, "ls-remote", "--exit-code", args.fork_url, "refs/heads/main").split()[0]
    if published_main != candidate_sha:
        raise PipelineError(
            f"Publication did not leave main at candidate {candidate_sha}; observed {published_main}"
        )
    print(f"Published checked candidate {candidate_sha} to {FORK_REPOSITORY}:main.")
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)

    stage_parser = commands.add_parser("stage", help="stage and check a candidate without touching published main")
    stage_parser.add_argument("--work-dir", type=Path, required=True)
    stage_parser.add_argument("--artifacts", type=Path, required=True)
    stage_parser.add_argument("--checker", type=Path, default=Path(__file__).with_name("check_optchat_candidate.py"))
    stage_parser.add_argument("--fork-url", default=FORK_URL)
    stage_parser.add_argument("--upstream-url", default=UPSTREAM_URL)
    stage_parser.add_argument("--plugin-url", default=PLUGIN_URL)
    stage_parser.add_argument("--plugin-sha", default=PINNED_PLUGIN_COMMIT)
    stage_parser.add_argument("--fork-repository", default=FORK_REPOSITORY)
    stage_parser.add_argument("--upstream-repository", default=UPSTREAM_REPOSITORY)
    stage_parser.add_argument("--plugin-repository", default=PLUGIN_REPOSITORY)
    stage_parser.add_argument("--upstream-branch", choices=("main",), default="main")
    stage_parser.add_argument(
        "--install-candidate-dependencies",
        action="store_true",
        help="check the candidate lock and build its isolated dev/test environment before running the checker",
    )
    stage_parser.set_defaults(function=stage)

    publish_parser = commands.add_parser("publish", help="verify evidence and compare-and-swap update main")
    publish_parser.add_argument("--artifacts", type=Path, required=True)
    publish_parser.add_argument("--work-dir", type=Path, required=True)
    publish_parser.add_argument("--checker", type=Path, default=Path(__file__).with_name("check_optchat_candidate.py"))
    publish_parser.add_argument("--fork-url", default=FORK_URL)
    publish_parser.add_argument("--plugin-sha", default=PINNED_PLUGIN_COMMIT)
    publish_parser.set_defaults(function=publish)
    return root


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return args.function(args)
    except (PipelineError, OSError, subprocess.SubprocessError) as exc:
        print(f"tested-fork pipeline: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
