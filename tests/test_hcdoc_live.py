"""Live tests for scripts/hcdoc.py against a real herdr-core in an isolated session.

Skipped when `hc` is not installed or HCDOC_NO_LIVE=1. Never touches your real session: it sets its own
HC_SESSION and scratch XDG dirs (kept short, unix socket paths are limited to ~103 bytes), and stops
only that core afterwards. No LLM calls, no mainframe.
"""
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
HCDOC = Path(os.environ.get("HCDOC_SCRIPT") or HERE.parent / "scripts" / "hcdoc.py")
HC = shutil.which("hc")


@unittest.skipUnless(HC and os.environ.get("HCDOC_NO_LIVE") != "1", "hc not installed or HCDOC_NO_LIVE=1")
class Live(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="dsl", dir="/tmp")
        cls.env = {k: v for k, v in os.environ.items()
                   if not k.startswith(("HC_", "HCDOC_")) and k != "XDG_RUNTIME_DIR"}
        cls.env.update(HC_SESSION=f"dsl{os.getpid() % 100000}", XDG_STATE_HOME=f"{cls.tmp}/s",
                       XDG_CONFIG_HOME=f"{cls.tmp}/c")
        r = cls.hc("ls", "--json")
        assert r.returncode == 0, r.stderr

    @classmethod
    def tearDownClass(cls):
        cls.hc("server", "stop")
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def hc(cls, *args):
        return subprocess.run([HC, *args], capture_output=True, text=True, env=cls.env, timeout=60)

    def hcdoc(self, *args, stdin=None, env=None):
        return subprocess.run([sys.executable, "-I", str(HCDOC), *args], capture_output=True, text=True,
                              env={**self.env, **(env or {})}, input=stdin, timeout=60)

    def get(self, name):
        p = self.hcdoc("get", name)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def put(self, name, value, version=0):
        p = self.hcdoc("set", name, "-", "--version", str(version), stdin=json.dumps(value))
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)["version"]

    def job(self, **kw):
        return {"schema": 1, "state": "pending", "owner": None, "attempt": 0, "max_attempts": 3,
                "started_ms": None, "finished_ms": None, "result": {"out": None, "summary": None, "error": None}, **kw}

    # ---------------------------------------------------------------- tests

    def test_roundtrip_and_conflict(self):
        self.assertEqual(self.get("sk-rt"), {"value": None, "version": 0})
        self.assertEqual(self.put("sk-rt", {"schema": 1, "a": 1}), 1)
        p = self.hcdoc("set", "sk-rt", "-", "--version", "0", stdin='{"schema":1,"a":2}')
        self.assertEqual(p.returncode, 3, p.stderr)
        self.assertEqual(self.get("sk-rt")["value"]["a"], 1)

    def test_validation_refused_and_nothing_written(self):
        for name in ("herdr-app", "SK-X", "sk-a.b", "sk-" + "x" * 30, "other-1", "t7"):
            p = self.hcdoc("set", name, "-", "--version", "0", stdin='{"schema":1}')
            self.assertEqual(p.returncode, 2, (name, p.stderr))
        p = self.hcdoc("get", "herdr-app")
        self.assertEqual(p.returncode, 2)
        big = json.dumps({"schema": 1, "x": "a" * 1_100_000})
        p = self.hcdoc("set", "sk-big", "-", "--version", "0", stdin=big)
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertIn("file path", p.stderr)
        for bad in ('{"a":1}', "[1]", "not json", '{"schema":2}', "null"):
            self.assertEqual(self.hcdoc("set", "sk-big", "-", "--version", "0", stdin=bad).returncode, 2, bad)
        self.assertEqual(self.get("sk-big"), {"value": None, "version": 0})

    def test_delete_keeps_version(self):
        v = self.put("sk-del", {"schema": 1})
        p = self.hcdoc("delete", "sk-del", "--version", str(v))
        self.assertEqual(p.returncode, 0, p.stderr)
        after = self.get("sk-del")
        self.assertEqual(after["value"], None)
        self.assertGreater(after["version"], 0)
        self.assertEqual(after["version"], v + 1)
        stale = self.hcdoc("set", "sk-del", "-", "--version", str(v), stdin='{"schema":1}')
        self.assertEqual(stale.returncode, 3, stale.stderr)
        self.assertEqual(self.hcdoc("delete", "sk-del", "--version", str(v)).returncode, 3)
        self.assertEqual(self.hcdoc("set", "sk-del", "-", "--version", "0", stdin='{"schema":1}').returncode, 3)
        self.assertEqual(self.put("sk-del", {"schema": 1, "again": True}, after["version"]), v + 2)

    def test_claim_race_exactly_one_wins(self):
        for n in (2, 3, 5):
            name = f"sk-race{n}"
            self.put(name, self.job())
            with ThreadPoolExecutor(n) as ex:
                res = list(ex.map(lambda i: (i, self.hcdoc("claim", name, "--owner", f"t{i + 1}")), range(n)))
            wins = [i for i, p in res if p.returncode == 0]
            self.assertEqual(len(wins), 1, [(i, p.returncode, p.stderr) for i, p in res])
            self.assertTrue(all(p.returncode == 3 for i, p in res if i not in wins))
            doc = self.get(name)["value"]
            self.assertEqual((doc["state"], doc["attempt"], doc["owner"]), ("running", 1, f"t{wins[0] + 1}"))

    def test_concurrent_updates_all_apply(self):
        self.put("sk-cnt", {"schema": 1, "n": 0})
        with ThreadPoolExecutor(5) as ex:
            res = list(ex.map(lambda i: self.hcdoc("update", "sk-cnt", "--incr", "n"), range(5)))
        self.assertEqual([p.returncode for p in res], [0] * 5, [p.stderr for p in res])
        self.assertEqual(self.get("sk-cnt")["value"]["n"], 5)

    def test_update_if_guards_a_zombie(self):
        self.put("sk-z", self.job())
        self.assertEqual(self.hcdoc("claim", "sk-z", "--owner", "t1").returncode, 0)
        p = self.hcdoc("update", "sk-z", "--if-str", "owner=t2", "--set-str", "state=done")
        self.assertEqual(p.returncode, 3, p.stderr)
        p = self.hcdoc("update", "sk-z", "--if-str", "owner=t1", "--if", "attempt=1", "--set-str", "state=done",
                       "--set-str", "result.out=runs/e/j.json", "--now", "finished_ms")
        self.assertEqual(p.returncode, 0, p.stderr)
        v = self.get("sk-z")["value"]
        self.assertEqual((v["state"], v["result"]["out"]), ("done", "runs/e/j.json"))
        self.assertIsInstance(v["finished_ms"], int)

    def test_socket_transport(self):
        self.put("sk-sock", {"schema": 1, "via": "connect"})
        env = {"HCDOC_TRANSPORT": "socket"}
        self.assertEqual(json.loads(self.hcdoc("get", "sk-sock", env=env).stdout)["value"]["via"], "connect")
        p = self.hcdoc("set", "sk-sock", "-", "--version", "1", stdin='{"schema":1,"via":"socket"}', env=env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.get("sk-sock")["value"]["via"], "socket")
        # auto falls back to the socket when `hc connect` cannot run
        p = self.hcdoc("get", "sk-sock", env={"HCDOC_CONNECT": "/nonexistent"})
        self.assertEqual(p.returncode, 0, p.stderr)
        # a dead socket is "core unavailable"
        p = self.hcdoc("get", "sk-sock", env={"HCDOC_TRANSPORT": "socket", "HC_SOCKET": f"{self.tmp}/nope.sock"})
        self.assertEqual(p.returncode, 4)

    def test_export_and_foreign_names(self):
        self.put("sk-x1", {"schema": 1, "exp": "x1", "jobs": ["sk-x1-j001", "sk-x1-j002", "sk-x1-j003"]})
        self.put("sk-x1-j001", self.job(state="done"))
        self.put("sk-x1-j002", self.job(state="failed"))
        out = Path(self.tmp) / "x1.json"
        p = self.hcdoc("export", "x1", str(out))
        self.assertEqual(p.returncode, 0, p.stderr)
        d = json.loads(out.read_text())
        self.assertEqual(sorted(d["jobs"]), ["sk-x1-j001", "sk-x1-j002"])
        self.assertEqual(d["missing"], ["sk-x1-j003"])
        self.assertEqual(d["manifest"]["exp"], "x1")
        self.put("sk-x2", {"schema": 1, "jobs": ["sk-x1-j001"]})
        bad = Path(self.tmp) / "x2.json"
        self.assertEqual(self.hcdoc("export", "x2", str(bad)).returncode, 2)
        self.assertFalse(bad.exists())
        self.assertEqual(self.hcdoc("export", "nope", str(bad)).returncode, 2)
        self.assertEqual(self.hcdoc("export", "../x", str(bad)).returncode, 2)

    def test_watch_streams_docs_and_labeled_terminals(self):
        proc = subprocess.Popen([sys.executable, "-I", str(HCDOC), "watch", "--label", "exp=w1", "--timeout", "40"],
                                stdout=subprocess.PIPE, text=True, env=self.env)
        self.addCleanup(lambda: (proc.kill(), proc.wait(), proc.stdout.close()))
        lines = queue.Queue()
        threading.Thread(target=lambda: [lines.put(json.loads(l)) for l in proc.stdout], daemon=True).start()

        def until(pred, what, secs=15):
            end, seen = time.time() + secs, []
            while time.time() < end:
                try:
                    m = lines.get(timeout=0.5)
                except queue.Empty:
                    continue
                seen.append(m)
                if pred(m):
                    return m
            self.fail(f"no {what}; saw {seen}")

        snap = until(lambda m: m["kind"] == "snapshot", "snapshot")
        self.assertEqual((snap["reason"], snap["terminals"]), ("subscribe", []))
        v = self.put("sk-w1", {"schema": 1})
        until(lambda m: m["kind"] == "doc" and m["name"] == "sk-w1" and m["version"] == v, "doc event")
        self.assertEqual(self.hc("run", "--bg", "--name", "other", "--", "sleep", "60").returncode, 0)
        r = self.hc("run", "--bg", "--json", "--name", "mine", "--label", "exp=w1", "--", "sleep", "60")
        mine = json.loads(r.stdout)["address"]
        other = json.loads(self.hc("get", "other", "--json").stdout)["address"]
        self.hc("kill", "other")
        self.hc("kill", "mine")
        fin = until(lambda m: m["kind"] == "terminal" and m["type"] == "finished", "finished event")
        self.assertEqual(fin["terminal"], mine)
        self.assertNotEqual(fin["terminal"], other)
        proc.kill()
        got = []
        while not lines.empty():
            got.append(lines.get())
        self.assertFalse([m for m in got if m.get("terminal") == other], got)


if __name__ == "__main__":
    unittest.main()
