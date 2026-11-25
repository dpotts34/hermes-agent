#!/usr/bin/env python3
"""One OptChat chat continued by two Hermes apps: the gateway (what Hermex talks to) and a Desktop
``hermes serve`` backend, as real processes on one Hermes home. Each app must release the chat's
memory once idle so the other can continue it. Synthetic data and a loopback model only."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

FACT = "Synthetic archive fact: the project verification code is ORCHID-17."
IDLE = 3
TURN_S = 120


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core", type=Path, required=True)
    parser.add_argument("--plugin", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--without-idle-setting", action="store_true",
                        help="Omit idle_release_seconds, for plugins that predate it")
    args = parser.parse_args()
    core, plugin, root = (path.resolve() for path in (args.core, args.plugin, args.artifacts))
    root.mkdir(parents=True, exist_ok=False)
    sys.path[:0] = [str(core)]
    from websockets.sync.client import connect
    from tests.e2e.core.chaos._helpers import hermetic_env, kill_tagged, new_tag, write_chaos_home
    from tests.fakes.fake_llm_provider import FakeLLMServer, Text, ToolCall
    from session_rules.turns import decode_content

    def answer(record):
        messages = record["body"].get("messages", [])
        last = messages[-1]
        assert any("OptChat supplies" in str(m.get("content", "")) for m in messages), "OptChat did not select the context"
        if last.get("role") == "tool":
            payload = json.loads(decode_content(last.get("content")))
            if "matches" in payload:
                return ToolCall("optchat_zoom", {"id": 0, "n": 1})
            assert FACT in payload.get("text", ""), payload
            return Text("ARCHIVE_RETRIEVAL_OK")
        text = str(decode_content(last.get("content")))
        if "Retrieve" in text:
            return ToolCall("optchat_search", {"query": "ORCHID-17"})
        return Text("RECORD_SAVED")

    def summary(record):
        return Text("Synthetic memory: project verification code ORCHID-17.")

    idle = "" if args.without_idle_setting else f"  idle_release_seconds: {IDLE}\n"
    # The harness config already disables compression and the built-in memory.
    extra = ("auxiliary:\n  title_generation:\n    enabled: false\n"
             "plugins:\n  enabled: [optchat]\n  isolation: in_process\n"
             "context:\n  engine: optchat\n"
             "optchat:\n  compression_policy: on_demand\n  chunk_limit: bytes\n  summary_concurrency: 1\n"
             "  merge_lanes: 1\n  summary_timeout: 20\n  settle_seconds: 30\n" + idle)
    result = {"passed": False, "surface": "real hermes gateway + Desktop hermes serve, one chat",
              "provider": "deterministic loopback HTTP provider; no real credentials", "turns": []}
    began, tag, procs, sockets = time.monotonic(), new_tag(), {}, []
    log = root / "procs.log"
    out = open(log, "wb")
    try:
        with FakeLLMServer(script=answer, aux=summary) as model:
            home, hermes_home = write_chaos_home(root, model.base_url, stale_timeout=600, request_timeout=600,
                                                 extra=extra)
            (hermes_home / "plugins").mkdir()
            (hermes_home / "plugins" / "optchat").symlink_to(plugin / "optchat", target_is_directory=True)
            env = hermetic_env(home, hermes_home, tag)
            for key in ("NOTIFY_SOCKET", "INVOCATION_ID", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
                        "XDG_CACHE_HOME", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"):
                env.pop(key, None)
            env.update(HERMES_GATEWAY_LOCK_DIR=str(root / "locks"), HERMES_GATEWAY_MAX_STARTS="0",
                       PYTHONPATH=f"{core}", HERMES_DISABLE_LAZY_INSTALLS="1", PYTHONDONTWRITEBYTECODE="1")

            def spawn(name, *argv, **extra_env):
                procs[name] = subprocess.Popen([sys.executable, "-B", "-m", "hermes_cli.main", *argv], cwd=str(home),
                                               env={**env, **extra_env}, stdin=subprocess.DEVNULL, stdout=out,
                                               stderr=subprocess.STDOUT)
                return procs[name]

            def wait_for(predicate, what, timeout=180):
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    if (value := predicate()):
                        return value
                    time.sleep(0.2)
                raise AssertionError(f"{what} timed out\n{log.read_text(errors='replace')[-3000:]}")

            def control(verb, params=None):
                from gateway.control_socket import resolve_client_socket_path
                path = resolve_client_socket_path(hermes_home)
                if path is None:
                    raise OSError("control socket absent")
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
                    peer.settimeout(10)
                    peer.connect(str(path))
                    peer.sendall(json.dumps({"protocol": 1, "id": 1, "verb": verb, "params": params or {}}).encode() + b"\n")
                    with peer.makefile("rb") as stream:
                        reply = json.loads(stream.readline())
                assert reply.get("ok"), reply
                return reply["result"]

            def identify():
                import os
                os.environ["HERMES_HOME"] = str(hermes_home)
                try:
                    desc = control("identify")
                except (OSError, ValueError, AssertionError):
                    return None
                return desc if desc.get("state") == "ready" else None

            counter = iter(range(1, 10_000))

            def rpc(ws, method, params):
                rid = next(counter)
                ws.send(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}))
                while True:
                    msg = json.loads(ws.recv(timeout=TURN_S))
                    if msg.get("id") == rid and "method" not in msg:
                        assert "result" in msg, f"{method}: {msg}"
                        return msg["result"]

            def turn(app, ws, sid, text):
                params = {"session_id": sid, "text": text}
                if app == "gateway":
                    params["input_id"] = f"two-app-{len(result['turns'])}"  # the gateway's idempotent admission key
                rpc(ws, "prompt.submit", params)
                while True:
                    msg = json.loads(ws.recv(timeout=TURN_S))
                    params = msg.get("params") or {}
                    if params.get("type") in {"message.complete", "error"}:
                        payload = params.get("payload") or {}
                        entry = {"app": app, "status": payload.get("status"), "text": str(payload.get("text", ""))[:300]}
                        result["turns"].append(entry)
                        assert payload.get("status") == "complete", entry
                        return entry

            spawn("gateway", "gateway", "run", "--quiet")
            desc = wait_for(lambda: identify() or (procs["gateway"].poll() is not None and {"exited": True}),
                            "gateway identify")
            assert "exited" not in desc, log.read_text(errors="replace")[-3000:]
            grant = control("session-ticket", {"profile_id": str(hermes_home.resolve()),
                                               "instance_id": desc["instance_id"], "purpose": "interactive"})
            url = desc["api_origin"].replace("http:", "ws:") + "/api/ws"
            gws = connect(url, subprotocols=["hermes-gateway-v1", "hermes-gateway-ticket." + grant["ticket"]],
                          open_timeout=60, max_size=None)
            sockets.append(gws)
            sid = rpc(gws, "session.create", {"request_id": "two-app", "source": "cli"})["session_id"]
            turn("gateway", gws, sid, f"Store this synthetic record. {FACT}")

            time.sleep(IDLE + 3)  # the gateway goes idle; Desktop continues the same chat
            token = secrets.token_urlsafe(24)
            spawn("desktop", "serve", "--host", "127.0.0.1", "--port", "0", HERMES_DASHBOARD_SESSION_TOKEN=token)
            port = int(wait_for(lambda: (m := re.search(rb"backend listening on [\d.]+:(\d+)", log.read_bytes()))
                                and m.group(1) or (procs["desktop"].poll() is not None and b"-1"), "serve port"))
            assert port > 0, log.read_text(errors="replace")[-3000:]
            dws = connect(f"ws://127.0.0.1:{port}/api/ws?token={token}", open_timeout=90, max_size=None)
            sockets.append(dws)
            runtime = rpc(dws, "session.resume", {"session_id": sid, "eager_build": True})["session_id"]
            turn("desktop", dws, runtime, "Store this second synthetic record from Desktop.")

            time.sleep(IDLE + 3)  # Desktop goes idle; the gateway (Hermex) continues
            retrieval = turn("gateway", gws, sid, "Retrieve the exact earlier synthetic project verification code.")
            assert "ARCHIVE_RETRIEVAL_OK" in retrieval["text"], retrieval

            with sqlite3.connect(hermes_home / "state.db") as db:
                failed = db.execute("SELECT count(*) FROM messages WHERE session_id=? AND display_kind='failed_turn'",
                                    (sid,)).fetchone()[0]
                assert failed == 0, f"{failed} failed turns"
                assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            archive = hermes_home / "optchat" / hashlib.sha256(sid.encode()).hexdigest() / "chat.db"
            with sqlite3.connect(archive) as db:
                assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                result["archive_events"] = db.execute("SELECT count(*) FROM events").fetchone()[0]
            result.update(passed=True, summary_requests=len(model.aux_requests()))
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"[:2000]
        raise
    finally:
        for ws in sockets:
            ws.close()
        for proc in procs.values():
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=10)
        kill_tagged(tag)
        out.close()
        result["seconds"] = round(time.monotonic() - began, 3)
        (root / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2), flush=True)


if __name__ == "__main__":
    main()
