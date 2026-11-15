#!/usr/bin/env python3
"""Check the exact core/plugin pair with isolated upstream tests and a real Desktop backend."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

CORE_TESTS = [
    "tests/agent/test_context_engine.py",
    "tests/agent/test_context_engine_select_context.py",
    "tests/agent/test_context_engine_message_uid.py",
    "tests/agent/test_context_engine_host_callbacks.py",
    "tests/agent/test_plugin_context_engine_clone.py",
    "tests/agent/test_plugin_context_engine_init.py",
    "tests/agent/test_context_engine_host_contract.py",
    "tests/agent/test_context_engine_on_turn_complete_usage.py",
    "tests/agent/test_commit_memory_session_context_engine.py",
    "tests/tui_gateway/test_compression_config_hot_reload.py",
    "tests/tui_gateway/test_context_engine_plugin_live_reload.py",
    "tests/tui_gateway/test_live_config_context_pin_mirror.py",
    "tests/tui_gateway/test_fallback_chain_hot_reload.py",
    "tests/agent/test_anthropic_preserved_thinking_replay.py",
    "tests/hermes_cli/test_tested_fork_updates.py",
    "tests/hermes_cli/test_update_fork_sync_identity.py",
    "tests/hermes_cli/test_update_narrow_clone_refspec.py",
]


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True,
                          text=True).stdout.strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core", type=Path, required=True)
    parser.add_argument("--plugin", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    args = parser.parse_args()
    core, plugin, artifacts = (path.resolve() for path in (args.core, args.plugin, args.artifacts))
    artifacts.mkdir(parents=True, exist_ok=False)
    policy = json.loads((core / ".optchat-update-policy.json").read_text())
    core_sha, plugin_sha = git(core, "rev-parse", "HEAD"), git(plugin, "rev-parse", "HEAD")
    if plugin_sha != policy["plugin_commit"]:
        raise SystemExit("The plugin checkout is not the policy's pinned commit")
    for checkout in (core, plugin):
        if git(checkout, "status", "--porcelain", "--untracked-files=no"):
            raise SystemExit(f"Refusing to check modified tracked source: {checkout}")
    home = artifacts / "home"
    guard_dir = home / ".hermes"
    guard_dir.mkdir(parents=True)
    temporary = artifacts / "tmp"
    temporary.mkdir()
    receipts = artifacts / "receipts"
    receipts.mkdir()
    guard = f'''import hashlib, json, os, sys
from pathlib import Path
sys.dontwritebytecode = True
CORE, PLUGIN, ARTIFACTS = map(Path, {repr([str(core),str(plugin),str(artifacts)])})
sys.path[:0] = [str(CORE), str(PLUGIN)]
os.environ["TMPDIR"] = str(ARTIFACTS / "tmp")
os.environ["HERMES_DISABLE_LAZY_INSTALLS"] = "1"
import agent.context_engine, optchat.store, run_agent
SOURCES = {{m.__name__: str(Path(m.__file__).resolve()) for m in (agent.context_engine,optchat.store,run_agent)}}
for name, source in SOURCES.items():
    assert Path(source).is_relative_to(PLUGIN if name.startswith("optchat.") else CORE), SOURCES
KEY = hashlib.sha256("\\0".join(sys.argv).encode()).hexdigest()[:20]
def pytest_configure(config):
    parent = ARTIFACTS / "pytest-temp"
    parent.mkdir(exist_ok=True)
    config.option.basetemp = str(parent / KEY)
def pytest_sessionfinish(session, exitstatus):
    result = {{"arguments":sys.argv,"exitstatus":int(exitstatus),"collected":session.testscollected,"sources":SOURCES,
               "runner_scratch":os.environ.get("PYTEST_DEBUG_TEMPROOT")}}
    (ARTIFACTS / "receipts" / (KEY + ".json")).write_text(json.dumps(result,indent=2)+"\\n")
'''
    (guard_dir / "pytest_live_guard.py").write_text(guard)
    wrapper = artifacts / "python"
    wrapper.write_text("#!/bin/sh\nexport PYTHONDONTWRITEBYTECODE=1\nexport TMPDIR=" +
                       shlex.quote(str(temporary)) + "\nexec " + shlex.quote(sys.executable) + ' -B "$@"\n')
    wrapper.chmod(0o700)
    env = {key: value for key, value in os.environ.items() if key in {
        "PATH", "LANG", "LC_ALL", "TERM", "SSL_CERT_FILE", "SSL_CERT_DIR", "LD_LIBRARY_PATH",
    }}
    env.update(HOME=str(home), HERMES_PYTHON=str(wrapper), PYTHONDONTWRITEBYTECODE="1",
               TMPDIR=str(temporary), HERMES_TEST_SCRATCH_ROOT=str(artifacts / "runner-scratch"), TZ="UTC")
    tests = [str(path) for path in sorted((plugin / "tests").rglob("test_*.py"))]
    tests.extend(str(core / path) for path in CORE_TESTS)
    for path in tests:
        if not Path(path).is_file():
            raise SystemExit(f"A required compatibility check is missing: {path}")
    result = {"passed": False, "core_commit": core_sha, "plugin_commit": plugin_sha}
    started = time.monotonic()
    try:
        commands = [
            ("target-verification", [sys.executable, "-B", "-m", "hermes_cli.tested_update_source",
                                     "--root", str(core), "--target", core_sha]),
            ("compatibility", ["bash", "scripts/run_tests.sh", *tests, "-j", "2", "--file-retries", "0",
                               "-q", "-p", "no:cacheprovider"]),
            ("desktop", [sys.executable, "-B", str(Path(__file__).with_name("optchat_desktop_smoke.py")),
                         "--core", str(core), "--plugin", str(plugin), "--artifacts", str(artifacts / "desktop")]),
        ]
        for name, command in commands:
            run_env = dict(env, PYTHONPATH=os.pathsep.join([str(core),str(plugin)]))
            completed = subprocess.run(command, cwd=core, env=run_env, capture_output=True,
                                       text=True, timeout=1800, check=False)
            (artifacts / (name + ".log")).write_text(completed.stdout + "\n" + completed.stderr)
            print(f"{name}: exit {completed.returncode}", flush=True)
            if completed.returncode:
                print(completed.stdout + completed.stderr, flush=True)
                raise RuntimeError(f"{name} failed")
        evidence = [json.loads(path.read_text()) for path in receipts.glob("*.json")]
        if len(evidence) != len(tests) or any(row["exitstatus"] != 0 for row in evidence):
            raise RuntimeError("Missing or failed source-provenance receipts")
        for checkout, expected in ((core, core_sha), (plugin, plugin_sha)):
            if (git(checkout, "rev-parse", "HEAD") != expected or
                    git(checkout, "status", "--porcelain", "--untracked-files=no")):
                raise RuntimeError(f"Checks changed the selected source: {checkout}")
        if any(not Path(row["runner_scratch"]).is_relative_to(artifacts) for row in evidence):
            raise RuntimeError("The canonical runner wrote scratch outside the artifact directory")
        result["passed"] = True
        result["test_files"] = len(evidence)
        result["collected"] = sum(row["collected"] for row in evidence)
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        result["seconds"] = round(time.monotonic() - started, 3)
        (artifacts / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2), flush=True)


if __name__ == "__main__":
    main()
