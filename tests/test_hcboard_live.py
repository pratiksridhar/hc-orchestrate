"""Live tests for the board (hcboard.py + docstore_worker.py + hcdoc.py) against a real herdr-core.

Runs in an isolated session (own HC_SESSION, scratch XDG dirs, short /tmp path for the socket), stops only
that core afterwards. Jobs are tests/fake_job.sh: no LLM calls, no mainframe. Skipped without `hc` or with
HCDOC_NO_LIVE=1. Takes about two minutes.
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "scripts"
HCBOARD, HCDOC = SCRIPTS / "hcboard.py", SCRIPTS / "hcdoc.py"
HC = shutil.which("hc")


@unittest.skipUnless(HC and os.environ.get("HCDOC_NO_LIVE") != "1", "hc not installed or HCDOC_NO_LIVE=1")
class BoardLive(unittest.TestCase):
    counter = 0

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="dsb", dir="/tmp")
        cls.env = {k: v for k, v in os.environ.items()
                   if not k.startswith(("HC_", "HCDOC_", "HCBOARD_")) and k != "XDG_RUNTIME_DIR"}
        cls.env.update(HC_SESSION=f"dsb{os.getpid() % 100000}", XDG_STATE_HOME=f"{cls.tmp}/s",
                       XDG_CONFIG_HOME=f"{cls.tmp}/c")
        assert cls.hc("ls", "--json").returncode == 0

    @classmethod
    def tearDownClass(cls):
        cls.hc("server", "stop")
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def hc(cls, *args):
        return subprocess.run([HC, *args], capture_output=True, text=True, env=cls.env, timeout=60)

    # ---------------------------------------------------------------- helpers

    def setUp(self):
        BoardLive.counter += 1
        self.exp = f"e{BoardLive.counter}"
        self.repo = Path(self.tmp) / f"repo{BoardLive.counter}"
        self.repo.mkdir()
        for cmd in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"],
                    ["commit", "-q", "--allow-empty", "-m", "base"]):
            subprocess.run(["git", *cmd], cwd=self.repo, check=True, capture_output=True)
        shutil.copy(HERE / "fake_job.sh", self.repo / "fake_job.sh")
        self.joblog = self.repo / "jobs.log"
        self.procs = []
        self.addCleanup(self.kill_procs)

    def kill_procs(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
            p.wait()

    def write_spec(self, cases, variants=("v",), lo=0.5, hi=1.5, slow=6, workers=3, attempts=3, name="spec.json", **extra):
        spec = {"question": "test", "cases": list(cases), "variants": list(variants), "pre": {"cases": "x"},
                "job_cmd": ["env", f"FAKE_JOB_MIN={lo}", f"FAKE_JOB_MAX={hi}", f"FAKE_JOB_SLOW={slow}",
                            f"FAKE_JOB_LOG={self.joblog}", "./fake_job.sh", "{case}", "{variant}", "{out}"],
                "max_workers": workers, "max_attempts": attempts, **extra}
        path = self.repo / name
        path.write_text(json.dumps(spec))
        return str(path)

    def board(self, *args, check=True):
        p = subprocess.run([sys.executable, "-I", str(HCBOARD), *args], capture_output=True, text=True,
                           env=self.env, timeout=120, cwd=self.repo)
        if check and p.returncode != 0:
            self.fail(f"hcboard {args} exited {p.returncode}\n{p.stdout}\n{p.stderr}")
        return p

    def init(self, spec=None, exp=None, **kw):
        return self.board("init", exp or self.exp, "--spec", spec or self.write_spec(**kw))

    def start(self, exp=None, poll=3, log="orch.out"):
        out = open(self.repo / log, "ab")
        self.addCleanup(out.close)
        p = subprocess.Popen([sys.executable, "-I", str(HCBOARD), "run", exp or self.exp, "--cwd", str(self.repo),
                              "--poll", str(poll), "--timeout", "150"], stdout=out, stderr=subprocess.STDOUT,
                             env=self.env)
        self.procs.append(p)
        return p

    def events(self, log="orch.out"):
        out = []
        for line in (self.repo / log).read_text().splitlines():
            if line.startswith("{"):
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
        return out

    def finish(self, proc, secs=120):
        try:
            rc = proc.wait(secs)
        except subprocess.TimeoutExpired:
            proc.kill()
            self.fail("orchestrator did not finish:\n" + (self.repo / "orch.out").read_text()[-3000:])
        self.assertEqual(rc, 0, (self.repo / "orch.out").read_text()[-3000:])

    def status(self, exp=None):
        p = self.board("status", exp or self.exp, "--json", check=False)
        return json.loads(p.stdout) if p.returncode == 0 else None

    def wait_for(self, pred, secs=60, what="condition"):
        end = time.time() + secs
        while time.time() < end:
            v = pred()
            if v:
                return v
            time.sleep(0.3)
        self.fail(f"timed out waiting for {what}\n" + (self.repo / "orch.out").read_text()[-2000:]
                  if (self.repo / "orch.out").exists() else f"timed out waiting for {what}")

    def exported(self, exp=None):
        return json.loads((self.repo / "experiments" / f"{exp or self.exp}.json").read_text())

    def get_doc(self, name):
        p = subprocess.run([sys.executable, "-I", str(HCDOC), "get", name], capture_output=True, text=True, env=self.env)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def assert_docs_deleted(self, exp=None):
        exp = exp or self.exp
        d = self.exported(exp)
        for name in ["sk-" + exp] + list(d["jobs"]):
            doc = self.get_doc(name)
            self.assertIsNone(doc["value"], name)
            self.assertGreater(doc["version"], 0, name)  # deleted, version kept

    def runs(self):
        if not self.joblog.exists():
            return Counter()
        return Counter(tuple(l.split()[:2]) for l in self.joblog.read_text().splitlines())

    def live_workers(self, exp=None):
        r = self.hc("ls", "--json", "--label", f"hco.board={exp or self.exp}")
        return json.loads(r.stdout)["terminals"]

    # ---------------------------------------------------------------- tests

    def test_full_run_export_and_cleanup(self):
        self.hc("run", "--bg", "--name", "bystander-done", "--", "sh", "-c", "exit 0")
        self.hc("run", "--bg", "--name", "bystander-live", "--", "sleep", "300")
        cases, variants = ["PAYCALC", "ACCTUPD", "LEDGER"], ["cl-base", "cl-pofq", "cx-base", "cx-pofq"]
        self.init(cases=cases, variants=variants, workers=3)
        self.assertEqual(self.status()["manifest"]["state"], "planned")
        p = self.start()
        self.finish(p)
        out = (self.repo / "orch.out").read_text()
        self.assertIn(f"HCBOARD {self.exp} COMPLETE done=12 failed=0", out)
        # every job ran exactly once, on at most 3 workers
        runs = self.runs()
        self.assertEqual(len(runs), 12)
        self.assertEqual(set(runs.values()), {1}, runs)
        d = self.exported()
        self.assertEqual(len(d["jobs"]), 12)
        self.assertEqual({j["state"] for j in d["jobs"].values()}, {"done"})
        self.assertEqual({j["attempt"] for j in d["jobs"].values()}, {1})
        self.assertLessEqual(len({j["owner"] for j in d["jobs"].values()}), 3)
        self.assertEqual([e["name"] for e in self.events() if e["event"] == "worker_started"][:3],
                         [f"{self.exp}-w1", f"{self.exp}-w2", f"{self.exp}-w3"])
        for j in d["jobs"].values():
            self.assertTrue((self.repo / j["result"]["out"]).exists())
        # committed, docs deleted (versions kept), our finished workers removed, strangers untouched
        log = subprocess.run(["git", "log", "-1", "--format=%s", "--", f"experiments/{self.exp}.json"],
                             cwd=self.repo, capture_output=True, text=True).stdout.strip()
        self.assertEqual(log, f"board: export experiment {self.exp}")
        self.assert_docs_deleted()
        self.assertEqual(self.hc("ls", "--all", "--json", "--label", f"hco.board={self.exp}").stdout.count('"address"'), 0)
        names = {t["name"] for t in json.loads(self.hc("ls", "--all", "--json").stdout)["terminals"]}
        self.assertTrue({"bystander-done", "bystander-live"} <= names, names)
        self.hc("kill", "bystander-live")

    def test_crash_recovery_requeues_and_finishes(self):
        self.init(cases=["SLOW1", "a", "b", "c"], workers=2, slow=6)
        p = self.start()
        slow = self.wait_for(lambda: next((n for n, j in (self.status() or {"jobs": {}})["jobs"].items()
                                           if j["case"] == "SLOW1" and j["state"] == "running"), None), what="SLOW1 running")
        self.wait_for(lambda: self.runs()[("SLOW1", "v")] >= 1, what="SLOW1 to start")  # kill it mid-job
        owner = self.get_doc(slow)["value"]["owner"]
        self.assertEqual(self.hc("kill", owner).returncode, 0)
        self.finish(p)
        job = self.exported()["jobs"][slow]
        self.assertEqual((job["state"], job["attempt"]), ("done", 2))
        self.assertNotEqual(job["owner"], owner)
        self.assertEqual(self.runs()[("SLOW1", "v")], 2, self.joblog.read_text())
        rq = [e for e in self.events() if e["event"] == "requeued"]
        self.assertEqual([(e["job"], e["owner"], e["to"]) for e in rq], [(slow, owner, "pending")])
        self.assertEqual(len([e for e in self.events() if e["event"] == "worker_started"]), 3)  # 2 + a replacement
        self.assertEqual(self.exported()["manifest"]["state"], "done")  # archived is set after the export

    def test_job_that_always_fails_ends_failed_after_max_attempts(self):
        self.init(cases=["FAIL1", "ok1"], workers=1, attempts=3, lo=0.2, hi=0.4)
        self.finish(self.start())
        d = self.exported()
        by_case = {j["case"]: j for j in d["jobs"].values()}
        self.assertEqual((by_case["FAIL1"]["state"], by_case["FAIL1"]["attempt"]), ("failed", 3))
        self.assertIn("exit 1", by_case["FAIL1"]["result"]["error"])
        self.assertIn("failed on purpose", by_case["FAIL1"]["result"]["error"])
        self.assertEqual(by_case["ok1"]["state"], "done")
        self.assertEqual(self.runs()[("FAIL1", "v")], 3)
        self.assertIn(f"COMPLETE done=1 failed=1", (self.repo / "orch.out").read_text())

    def test_pause_stops_claims_and_resume_continues(self):
        self.init(cases=[f"c{i}" for i in range(8)], workers=2, lo=1.0, hi=1.5)
        p = self.start()
        self.wait_for(lambda: (self.status() or {"counts": {}})["counts"].get("done", 0) >= 1, what="first job done")
        self.board("pause", self.exp)
        self.wait_for(lambda: self.status()["counts"]["running"] == 0 and not self.live_workers(), what="workers drained")
        frozen = self.status()["counts"]
        self.assertGreater(frozen["pending"], 0, frozen)
        self.assertEqual(self.status()["manifest"]["state"], "paused")
        time.sleep(4)
        self.assertEqual(self.status()["counts"], frozen)  # nothing claimed, nothing started
        self.assertEqual(self.live_workers(), [])
        self.assertIsNone(p.poll(), "orchestrator must keep waiting while paused")
        self.board("resume", self.exp)
        self.finish(p)
        self.assertEqual({j["state"] for j in self.exported()["jobs"].values()}, {"done"})
        self.assertEqual(set(self.runs().values()), {1})

    def test_orchestrator_restart_reconciles_orphans(self):
        self.init(cases=[f"c{i}" for i in range(8)], workers=2, lo=2.0, hi=3.0)
        a = self.start(poll=2)
        running = self.wait_for(lambda: [(n, j) for n, j in (self.status() or {"jobs": {}})["jobs"].items()
                                         if j["state"] == "running"], what="a running job")
        a.send_signal(signal.SIGKILL)
        a.wait()
        victim_job, victim = running[0][0], running[0][1]["owner"]
        self.assertEqual(self.hc("kill", victim).returncode, 0)  # orphan: dead owner, nobody watching
        time.sleep(1)
        self.assertEqual(self.get_doc(victim_job)["value"]["state"], "running")
        b = self.start(poll=2, log="orch2.out")
        self.finish_log(b, "orch2.out")
        ev = self.events("orch2.out")
        self.assertIn(victim_job, [e["job"] for e in ev if e["event"] == "requeued"])
        d = self.exported()
        self.assertEqual({j["state"] for j in d["jobs"].values()}, {"done"})
        self.assertEqual(d["jobs"][victim_job]["attempt"], 2)
        self.assertEqual(len(d["jobs"]), 8)

    def finish_log(self, proc, log):
        try:
            rc = proc.wait(120)
        except subprocess.TimeoutExpired:
            proc.kill()
            self.fail((self.repo / log).read_text()[-3000:])
        self.assertEqual(rc, 0, (self.repo / log).read_text()[-3000:])

    def test_watch_connection_loss_resubscribes_and_reconciles(self):
        self.init(cases=[f"c{i}" for i in range(6)], workers=2, lo=1.0, hi=1.5)
        p = self.start(poll=5)
        self.wait_for(lambda: any(e["event"] == "worker_started" for e in self.events()), what="a worker")

        def watcher():
            r = subprocess.run(["pgrep", "-P", str(p.pid), "-f", "hcdoc.py"], capture_output=True, text=True)
            return r.stdout.split()

        pids = self.wait_for(watcher, what="the watcher process")
        os.kill(int(pids[0]), signal.SIGKILL)
        self.finish(p)
        self.assertIn("watch_restarted", [e["event"] for e in self.events()])
        self.assertEqual({j["state"] for j in self.exported()["jobs"].values()}, {"done"})
        self.assertEqual(set(self.runs().values()), {1})

    def test_lineage_and_child_experiment_event(self):
        self.init(cases=["SLOW1", "a"], workers=2, slow=5)
        p = self.start()
        self.wait_for(lambda: any(e["event"] == "worker_started" for e in self.events()), what="a worker")
        child = f"{self.exp}c"
        r = self.board("init", child, "--spec", self.write_spec(cases=["a"], name="spec2.json"), "--parent", self.exp,
                       "--delta", '{"macros": true}')
        self.assertEqual(json.loads(r.stdout)["manifest"], f"sk-{child}")
        m = self.status(child)["manifest"]
        self.assertEqual((m["parent"], m["delta"], m["state"]), (self.exp, {"macros": True}, "planned"))
        self.finish(p)
        ce = [e for e in self.events() if e["event"] == "child_experiment"]
        self.assertEqual([(e["exp"], e["delta"]) for e in ce], [(child, {"macros": True})])
        self.assertEqual(self.status(child)["manifest"]["state"], "planned")  # never started by the parent's loop

    def test_init_refusals(self):
        self.init(cases=["a"])
        self.assertEqual(self.board("init", self.exp, "--spec", self.write_spec(cases=["a"]), check=False).returncode, 2)
        self.assertEqual(self.board("init", "e99", "--spec", self.write_spec(cases=["a"]), "--delta", "{}",
                                    check=False).returncode, 2)
        bad = self.write_spec(cases=["a"], name="bad.json")
        spec = json.loads(Path(bad).read_text())
        spec["job_cmd"] = ["zowe", "files", "list"]
        Path(bad).write_text(json.dumps(spec))
        self.assertEqual(self.board("init", "e98", "--spec", bad, check=False).returncode, 2)
        self.assertIsNone(self.status("e98"))
        self.assertEqual(self.board("status", "nope", check=False).returncode, 2)
        self.assertEqual(self.board("pause", self.exp, check=False).returncode, 1)  # still `planned`, not running


if __name__ == "__main__":
    unittest.main()
