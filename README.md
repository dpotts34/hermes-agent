# Tested-fork maintenance control

This source is intended for the `optchat-maintenance` control branch. The runtime updater remains on `dpotts34/hermes-agent:main`; this workflow does not deploy to an installed Hermes tree or change the live plugin.

## Defaults and policy

- Runtime fork: `dpotts34/hermes-agent`, branch `main`.
- Official source: `NousResearch/hermes-agent`, branch `main`; the selected upstream SHA is the exact commit fetched from `refs/heads/main` for that run. There is no release tag (`upstream_tag` is JSON `null`).
- Plugin source: `dpotts34/Hermes-OptChat-Plugin`, pinned to `b41dee60c4328137cff18c34cb7cab0b352e9e1a`.
- Initial seed: `dpotts34/hermes-agent` branch `optchat/seed`.
- Runtime policy: `.optchat-update-policy.json` with exactly these keys and values:

  ```json
  {
    "schema": 1,
    "repository": "dpotts34/hermes-agent",
    "branch": "main",
    "upstream_repository": "NousResearch/hermes-agent",
    "upstream_branch": "main",
    "plugin_repository": "dpotts34/Hermes-OptChat-Plugin",
    "plugin_commit": "b41dee60c4328137cff18c34cb7cab0b352e9e1a"
  }
  ```

The automation fetches only the named fork branches, official `main`, and the pinned plugin commit (no tags or unrelated branch refs). If runtime `main` has neither a tested-fork marker in its ancestry nor the policy file at its tip, it fetches `optchat/seed` and requires the currently published `main` to be an ancestor of that seed. A policy-containing but unmarked `main` is staged from its own tip; it is never silently replaced by the seed. Invalid/missing policy on a selected base fails closed.

An unmarked candidate receives its promotion metadata commit **before** the checker runs. The exact SHA of that commit is tested and bundled. No commit is added or amended after a successful check. The commit trailers record the updater-required marker, source/plugin/policy identities, checker digest, control revision, and prior `main` SHA. A no-op is allowed only when current `main` already contains the selected official commit and its tip trailers match the currently pinned plugin, raw policy SHA-256, checker version/digest, and control revision. The pinned plugin is still fetched and the policy is revalidated before that no-op is reported.

## Workflow

`.github/workflows/tested-fork-promotion.yml` runs daily and on manual dispatch. Both jobs are restricted to `refs/heads/optchat-maintenance`. GitHub runs scheduled workflows only from the repository's default branch, so the parent integration must make `optchat-maintenance` the repository default branch for the schedule to execute; otherwise manual dispatch from that branch remains available, but the schedule will not run.

The stage job has only `contents: read`, checks out the control revision with `persist-credentials: false`, and clones public source into `$RUNNER_TEMP`. Before tests, it runs the candidate's `pm.build_env --check-lock` and builds its isolated `dev`/`test` environment from the candidate source. Build HOME, Hermes runtime, and caches are confined to the staging work directory; the checker runs under the generated pinned interpreter and creates only a disposable fake HOME/test scratch under its evidence directory. Only checker evidence plus a candidate Git bundle and manifest are uploaded. The stage job does not receive a write token or private profile data. The checker contract is:

```text
python scripts/check_optchat_candidate.py --core PATH --plugin PATH --artifacts DIR
```

The separate publish job runs only for a successful promotion (not a no-op). It has `contents: write`, verifies the manifest, bundle checksum/ref, exact SHAs, policy bytes, marker trailers, and ancestry; then it updates `main` only if remote `main` still equals the manifest's `old_main_sha`. The compare-and-swap lease is paired with an explicit proof that candidate descends from that SHA, so any accepted update is fast-forward. A merge/check failure, artifact mismatch, or stale remote `main` leaves the published branch unchanged and fails the run. No temporary fork branch is created.

The evidence manifest is `manifest.json`, with `status.json` for the job gate. A promotion manifest records `old_main_sha`, `candidate_sha`, `upstream_channel`, `upstream_branch`, `upstream_tag`, `upstream_sha`, `plugin_sha`, raw `policy_sha256`, `checker_sha256`, `control_revision`, `python_version`, the checker result, and bundle checksum. A verified no-op emits a manifest/status artifact but no candidate bundle.

The checkout, upload, and download actions are pinned to immutable commits verified against their release tags. The workflow carries no repository secrets other than its job-scoped `GITHUB_TOKEN`; that token is exposed only to the publisher.

## Local commands and tests

The CLI supports local bare remotes for integration testing. For example, see the subprocess-based tests in `tests/test_tested_fork.py`; they create local bare fork/upstream/plugin repositories and a synthetic checker. The test cases cover a checker failure, a merge conflict, a post-check publication race, preserved successful history, and no-op verification.

The standard-library test command (for parent integration to run) is:

```sh
python3 -m unittest discover -s tests -v
```

Do not point staging at an installed runtime, profile, chat archive, or live plugin tree. The only stage inputs are the public fork/upstream commits, the pinned plugin commit, and the supplied checker.
