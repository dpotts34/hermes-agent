"""A tested downstream source must never silently update from raw upstream."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

POLICY_FILE = ".optchat-update-policy.json"
_SHA = re.compile(r"[0-9a-f]{40}")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


def _run_git(root: Path, args: list[str], git_cmd=None):
    from hermes_cli.source_check import source_git_env
    result = subprocess.run(
        [*(git_cmd or ["git"]), *args], cwd=root, env=source_git_env(),
        capture_output=True, stdin=subprocess.DEVNULL, timeout=20, check=False,
    )
    if result.returncode:
        raise ValueError(f"Cannot verify tested update source ({args[0]} failed)")
    return result


def _git(root: Path, args: list[str], git_cmd=None) -> str:
    return _run_git(root, args, git_cmd).stdout.decode("utf-8", errors="replace").strip()


def _decode(raw: bytes) -> dict:
    try:
        policy = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("Invalid tested-fork update policy") from exc
    if not isinstance(policy, dict) or type(policy.get("schema")) is not int or policy["schema"] != 1:
        raise ValueError("Unsupported tested-fork update policy")
    for field in ("repository", "upstream_repository", "plugin_repository"):
        if not isinstance(policy.get(field), str) or not _REPOSITORY.fullmatch(policy[field]):
            raise ValueError(f"Invalid tested-fork {field}")
    if policy.get("branch") != "main" or policy.get("upstream_branch") != "main":
        raise ValueError("The tested-fork update policy requires its main channel")
    if not isinstance(policy.get("plugin_commit"), str) or not _SHA.fullmatch(policy["plugin_commit"]):
        raise ValueError("The tested-fork plugin must be pinned to a full commit")
    return policy


def read_policy(root: Path, git_cmd=None) -> dict | None:
    """Read and bind an installed policy to its actual origin; absent means ordinary Hermes."""
    root = Path(root)
    path = root / POLICY_FILE
    if not path.exists():
        return None
    policy = _decode(path.read_bytes())
    from hermes_cli.source_releases import _GITHUB_ORIGIN
    origin = _git(root, ["config", "--get", "remote.origin.url"], git_cmd)
    match = _GITHUB_ORIGIN.fullmatch(origin)
    if not match or match[1].lower() != policy["repository"].lower():
        raise ValueError("Tested-fork origin does not match its update policy; no update is safe")
    return policy


def target_refusal(root: Path, target_ref: str, git_cmd=None) -> str | None:
    """Reject an untested target before the native updater changes the working tree."""
    try:
        current = read_policy(root, git_cmd)
        if current is None:
            return None
        target = _git(root, ["rev-parse", "--verify", "--end-of-options",
                             f"{target_ref}^{{commit}}"], git_cmd)
        if not _SHA.fullmatch(target):
            raise ValueError("The tested update target is not a commit")
        blob = _run_git(root, ["show", f"{target}:{POLICY_FILE}"], git_cmd).stdout
        if _decode(blob) != current:
            raise ValueError("The update changes the tested plugin/source policy; coordinated review is required")
        message = _git(root, ["log", "-1", "--format=%B", target], git_cmd)
        trailers = {}
        for line in message.splitlines():
            if line.startswith("OptChat-") and ": " in line:
                key, value = line.split(": ", 1)
                if key in trailers:
                    raise ValueError("The tested update has duplicate verification trailers")
                trailers[key] = value.strip()
        expected = {
            "OptChat-Tested-Fork": "true",
            "OptChat-Upstream-Channel": "main",
            "OptChat-Plugin-SHA": current["plugin_commit"],
            "OptChat-Policy-SHA256": hashlib.sha256(blob).hexdigest(),
        }
        if any(trailers.get(key) != value for key, value in expected.items()):
            raise ValueError("The update target has not passed the tested-fork promotion checks")
        if not _SHA.fullmatch(trailers.get("OptChat-Upstream-SHA", "")):
            raise ValueError("The update target does not identify its official upstream commit")
        return None
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return f"Tested-fork update refused: {exc}. The current installation is unchanged."


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--target", required=True)
    args = parser.parse_args()
    error = target_refusal(args.root, args.target)
    print(json.dumps({"allowed": error is None, "error": error}))
    raise SystemExit(1 if error else 0)


if __name__ == "__main__":
    main()
