#!/usr/bin/env python3
"""Exercise summary RPC, archive retrieval, and cold resume with synthetic data only."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
import time


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core", type=Path, required=True)
    parser.add_argument("--plugin", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    args = parser.parse_args()
    core, plugin, root = (path.resolve() for path in (args.core,args.plugin,args.artifacts))
    root.mkdir(parents=True, exist_ok=False)
    home = root / "hermes-home"
    sys.path[:0] = [str(core),str(plugin)]
    from tests.e2e.core.history._helpers import OFFLINE_CONFIG, Spawned, TuiGateway
    from tests.fakes.fake_llm_provider import FakeLLMServer, Text, ToolCall, write_hermes_home
    from hermes_state import SessionDB
    import agent.context_engine
    import optchat.store
    for module, source in ((agent.context_engine,core),(optchat.store,plugin)):
        assert Path(module.__file__).resolve().is_relative_to(source), module.__file__

    class DesktopGateway(TuiGateway):
        def start(self):
            from websockets.sync.client import connect
            token = secrets.token_urlsafe(32)
            self.token_path = root / "desktop-token"
            self.token_path.write_text(token)
            self.token_path.chmod(0o600)
            child_env = dict(self.env,HERMES_DESKTOP="1",
                             HERMES_DASHBOARD_SESSION_TOKEN="file:"+str(self.token_path))
            self.proc = self.spawned.add(subprocess.Popen(
                [sys.executable,"-B","-m","hermes_cli.main","serve","--isolated",
                 "--host","127.0.0.1","--port","0"], cwd=self.cwd, env=child_env,
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                text=True,encoding="utf-8",bufsize=1))
            self.socket = None
            ready = queue.Queue()

            def output(stream):
                for raw in stream:
                    line = raw.rstrip("\n")
                    self.stderr_lines.append(line.replace(token,"[redacted]"))
                    match = re.search(r"HERMES_BACKEND_READY port=(\d+)",line)
                    if match:
                        ready.put(int(match.group(1)))
                ready.put(None)

            for stream in (self.proc.stdout,self.proc.stderr):
                threading.Thread(target=output,args=(stream,),daemon=True).start()
            port = ready.get(timeout=120)
            if port is None:
                raise RuntimeError("Desktop exited before readiness")
            self.socket = connect(f"ws://127.0.0.1:{port}/api/ws?token={token}",
                                  origin=f"http://127.0.0.1:{port}",
                                  subprotocols=["hermes-gateway-v1"],max_size=None)
            threading.Thread(target=self.read_socket,daemon=True).start()

        def _write(self,frame):
            with self._wlock:
                self.socket.send(json.dumps(frame))

        def read_socket(self):
            try:
                for raw in self.socket:
                    frame = json.loads(raw)
                    if frame.get("method") == "event":
                        with self._cond:
                            self.events.append(frame["params"])
                            self._cond.notify_all()
                    elif "method" in frame and "id" in frame:
                        self._write({"jsonrpc":"2.0","id":frame["id"],
                                     "error":{"code":-32601,"message":"No client-side tools in this smoke"}})
                    elif "id" in frame and frame["id"] in self._pending:
                        self._pending[frame["id"]].put(frame)
            finally:
                with self._cond:
                    self._cond.notify_all()

        def close(self,timeout=40):
            if getattr(self,"socket",None) is not None:
                self.socket.close()
            if self.proc is not None and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=10)
            if getattr(self,"token_path",None) is not None:
                self.token_path.unlink(missing_ok=True)

    fact = "Synthetic archive fact: the project verification code is ORCHID-17."
    summary_requests = []
    main_requests = []

    def summary(request):
        summary_requests.append(request["body"])
        return Text("Synthetic memory: project verification code ORCHID-17. Preserve the raw archive for exact retrieval.")

    def answer(request):
        body = request.get("body",request)
        messages = body.get("messages",[])
        is_main = any("OptChat supplies" in str(message.get("content","")) for message in messages)
        assert is_main, "The main provider request did not use OptChat's selected context"
        main_requests.append(body)
        last = messages[-1]
        if last.get("role") == "tool":
            payload = SessionDB._decode_content(last.get("content"))
            if isinstance(payload,str):
                payload = json.loads(payload)
            if "matches" in payload:
                assert any(match.get("id") == 0 for match in payload["matches"]),payload
                return ToolCall("optchat_zoom",{"id":0,"n":1})
            assert fact in payload.get("text",""),payload
            return Text("ARCHIVE_RETRIEVAL_OK")
        if str(last.get("content","")).startswith("Store this synthetic record"):
            return Text("SYNTHETIC_RECORD_SAVED")
        assert any("<chat>" in str(message.get("content","")) for message in messages),messages
        return ToolCall("optchat_search",{"query":"ORCHID-17"})

    extra = OFFLINE_CONFIG + """
profiles:
  multiplex: false
plugins:
  enabled: [optchat]
  isolation: in_process
context:
  engine: optchat
memory:
  memory_enabled: false
  user_profile_enabled: false
  provider: none
  nudge_interval: 0
compression:
  enabled: false
skills:
  creation_nudge_interval: 0
mcp_servers: {}
optchat:
  view_limit: 32768
  compression_policy: on_demand
  chunk_limit: bytes
  summary_concurrency: 1
  merge_lanes: 1
  summary_timeout: 20
  settle_seconds: 30
"""
    env = {key:value for key,value in os.environ.items() if key in {
        "PATH","LANG","LC_ALL","TERM","SSL_CERT_FILE","SSL_CERT_DIR","LD_LIBRARY_PATH",
    }}
    env.update(HOME=str(root/"user-home"),HERMES_HOME=str(home),
               PYTHONPATH=os.pathsep.join([str(core),str(plugin)]),
               PYTHONDONTWRITEBYTECODE="1",HERMES_DISABLE_LAZY_INSTALLS="1",
               TMPDIR=str(root/"tmp"),TZ="UTC")
    Path(env["HOME"]).mkdir()
    Path(env["TMPDIR"]).mkdir()
    result = {"passed":False,"surface":"actual isolated Desktop HTTP/WebSocket backend",
              "provider":"deterministic loopback HTTP provider; no real credentials"}
    began = time.monotonic()
    try:
        with FakeLLMServer(script=answer,aux=summary) as provider:
            write_hermes_home(home,provider.base_url,extra_config=extra)
            (home/"plugins").mkdir()
            (home/"plugins"/"optchat").symlink_to(plugin/"optchat",target_is_directory=True)
            stored = None
            records = []
            seeds = [f"Store this synthetic record {index}. {fact}\n" + ("synthetic history block. " * 780)
                     for index in range(3)]
            for stage in ("first","cold-resume"):
                transport = DesktopGateway(env,root,Spawned())
                try:
                    transport.start()
                    transport.wait_event("gateway.ready",timeout=120)
                    if stored is None:
                        created = transport.call("session.create",{"cwd":str(root)},timeout=120)
                        runtime,stored = created["session_id"],created["stored_session_id"]
                        prompts = seeds
                    else:
                        resumed = transport.call("session.resume",{"session_id":stored,"eager_build":True},timeout=120)
                        runtime = resumed["session_id"]
                        prompts = ["Use OptChat search and zoom to retrieve the exact earlier synthetic project verification code."]
                    for prompt in prompts:
                        completion = transport.submit(runtime,prompt,timeout=120)
                        assert completion.get("status") == "complete",completion
                        records.append({"stage":stage,"completion":completion})
                finally:
                    transport.close()
                    (root/(stage+".log")).write_text("\n".join(transport.stderr_lines)+"\n")
            assert provider.aux_requests(), "The smoke did not exercise a real auxiliary summary HTTP request"
            with sqlite3.connect(home/"state.db") as database:
                database.row_factory = sqlite3.Row
                rows = [dict(row) for row in database.execute(
                    "SELECT role,content,tool_name FROM messages WHERE session_id=? AND active=1 ORDER BY id",(stored,))]
                tools = {row["tool_name"] for row in rows if row["role"] == "tool"}
                users = [SessionDB._decode_content(row["content"]) for row in rows if row["role"] == "user"]
                answers = [SessionDB._decode_content(row["content"]) for row in rows if row["role"] == "assistant"]
                assert users[:3] == seeds,"Core history did not preserve the original records"
                assert {"optchat_search","optchat_zoom"}.issubset(tools),tools
                assert answers[-1] == "ARCHIVE_RETRIEVAL_OK",answers
                assert database.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            archive = home/"optchat"/hashlib.sha256(stored.encode()).hexdigest()/"chat.db"
            with sqlite3.connect(archive) as database:
                assert database.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            (root/"provider-requests.json").write_text(json.dumps(provider.requests,indent=2)+"\n")
            result.update(passed=True,summary_requests=len(summary_requests),main_requests=len(main_requests),records=records)
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        result["seconds"] = round(time.monotonic()-began,3)
        (root/"result.json").write_text(json.dumps(result,indent=2)+"\n")
        print(json.dumps(result,indent=2),flush=True)


if __name__ == "__main__":
    main()
