"""End-to-end board tests without hc: hcboard.py, docstore_worker.py and hcdoc.py run for real against
tests/fake_core.py (a file-backed doc store with events, and the few `hc` commands the board uses).

Run: python3 -I -m unittest discover -s tests -v
The same scenarios against a real core are in test_hcboard_live.py (skipped without hc).
"""
import contextlib
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
SCRIPTS = Path(os.environ.get("HCB_SCRIPTS") or HERE.parent / "scripts")  # HCB_SCRIPTS: test another copy
HCBOARD, HCDOC, FAKE = SCRIPTS / "hcboard.py", SCRIPTS / "hcdoc.py", HERE / "fake_core.py"
sys.path.insert(0, str(SCRIPTS))
import docstore_worker as dw  # noqa: E402
import hcboard  # noqa: E402
import hcdoc  # noqa: E402

# A job that sleeps $2 seconds, fails if its case contains FAIL, and logs each start to jobs.log.
JOB = """#!/bin/sh
echo "$1 $3" >> jobs.log
sleep "$2"
case "$1" in *FAIL*) echo "boom $1" >&2; exit 1 ;; esac
printf '{"summary":"%s ok"}\\n' "$1" > "$4"
"""


class Offline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hcb")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = Path(self.tmp) / "repo"
        self.repo.mkdir()
        core = Path(self.tmp) / "core"
        core.mkdir()
        for cmd in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"],
                    ["commit", "-q", "--allow-empty", "-m", "base"]):
            subprocess.run(["git", *cmd], cwd=self.repo, check=True, capture_output=True)
        (self.repo / "job.sh").write_text(JOB)
        (self.repo / "job.sh").chmod(0o755)
        FAKE.chmod(0o755)
        self.core = core
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("HC_", "HCDOC_", "HCBOARD_"))}
        self.env.update(FAKE_CORE_DIR=str(core), HCBOARD_HC=str(FAKE), HCDOC_TRANSPORT="connect",
                        HCDOC_CONNECT=f"{shlex.quote(sys.executable)} {shlex.quote(str(FAKE))} connect")
        self.procs = []
        self.addCleanup(self.stop_everything)

    def stop_everything(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
            p.wait()
        for t in self.terminals():
            if t["state"] == "live" and t.get("pid"):
                try:
                    os.kill(t["pid"], 9)
                except OSError:
                    pass

    # ---------------------------------------------------------------- helpers

    def terminals(self):
        try:
            return list(json.loads((self.core / "terminals.json").read_text())["terminals"].values())
        except FileNotFoundError:
            return []

    def spec(self, cases, variants=("v",), secs=0.05, name="spec.json", **extra):
        spec = {"question": "q", "cases": list(cases), "variants": list(variants),
                "job_cmd": ["./job.sh", "{case}", str(secs), "{variant}", "{out}"],
                "max_workers": 2, "max_attempts": 2, **extra}
        (self.repo / name).write_text(json.dumps(spec))
        return str(self.repo / name)

    def board(self, *args, check=True, env=None):
        p = subprocess.run([sys.executable, "-I", str(HCBOARD), *args], capture_output=True, text=True,
                           env={**self.env, **(env or {})}, timeout=120, cwd=self.repo)
        if check and p.returncode != 0:
            self.fail(f"hcboard {args} exited {p.returncode}\n{p.stdout}\n{p.stderr}")
        return p

    def start(self, exp, *extra, env=None, cwd=None):
        out = open(self.repo / f"{exp}.out", "ab")
        self.addCleanup(out.close)
        p = subprocess.Popen([sys.executable, "-I", str(HCBOARD), "run", exp, "--cwd", str(cwd or self.repo),
                              "--poll", "2", "--timeout", "90", *extra], stdout=out, stderr=subprocess.STDOUT,
                             env={**self.env, **(env or {})}, cwd=self.repo)
        self.procs.append(p)
        return p

    def output(self, exp):
        return (self.repo / f"{exp}.out").read_text()

    def events(self, exp):
        out = []
        for line in self.output(exp).splitlines():
            if line.startswith("{"):
                out.append(json.loads(line))
        return out

    def finish(self, proc, exp, rc=0, secs=90):
        try:
            got = proc.wait(secs)
        except subprocess.TimeoutExpired:
            proc.kill()
            self.fail("orchestrator did not finish:\n" + self.output(exp)[-3000:])
        self.assertEqual(got, rc, self.output(exp)[-3000:])

    def run_board(self, exp, *extra, **kw):
        self.finish(self.start(exp, *extra, **kw), exp)
        return self.output(exp)

    def status(self, exp):
        p = self.board("status", exp, "--json", check=False)
        return json.loads(p.stdout) if p.returncode == 0 else None

    def wait_for(self, pred, secs=30, what="condition"):
        end = time.time() + secs
        while time.time() < end:
            v = pred()
            if v:
                return v
            time.sleep(0.1)
        self.fail(f"timed out waiting for {what}")

    def exported(self, exp):
        return json.loads((self.repo / "experiments" / f"{exp}.json").read_text())

    def doc(self, name):
        p = subprocess.run([sys.executable, "-I", str(HCDOC), "get", name], capture_output=True, text=True, env=self.env)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def starts(self):
        f = self.repo / "jobs.log"
        return f.read_text().split("\n")[:-1] if f.exists() else []

    def ops(self):
        return json.loads((self.core / "ops.json").read_text())

    # ---------------------------------------------------------------- the existing flow, now covered offline

    def test_full_run_exports_commits_and_cleans_up(self):
        self.board("init", "e1", "--spec", self.spec(["A", "B", "C"], ["v1", "v2"]))
        self.assertEqual(self.status("e1")["manifest"]["state"], "planned")
        out = self.run_board("e1")
        self.assertIn("HCBOARD e1 COMPLETE done=6 failed=0\n", out)
        self.assertEqual(sorted(self.starts()), sorted(f"{c} {v}" for c in "ABC" for v in ("v1", "v2")))  # once each
        d = self.exported("e1")
        self.assertEqual({j["state"] for j in d["jobs"].values()}, {"done"})
        self.assertEqual(d["jobs"]["sk-e1-j001"]["result"]["summary"], "A ok")
        self.assertEqual([e["name"] for e in self.events("e1") if e["event"] == "worker_started"][:2], ["e1-w1", "e1-w2"])
        log = subprocess.run(["git", "log", "-1", "--format=%s"], cwd=self.repo, capture_output=True, text=True).stdout
        self.assertEqual(log.strip(), "board: export experiment e1")
        self.assertEqual(self.doc("sk-e1"), {"value": None, "version": self.doc("sk-e1")["version"]})
        self.assertIsNone(self.doc("sk-e1-j003")["value"])
        self.assertEqual(self.terminals(), [])  # finished workers removed

    def test_killed_worker_is_requeued_and_the_job_runs_again(self):
        self.board("init", "e1", "--spec", self.spec(["SLOW"], secs=3, max_workers=1))
        p = self.start("e1")
        owner = self.wait_for(lambda: (self.status("e1") or {"jobs": {}})["jobs"].get("sk-e1-j001", {}).get("owner"),
                              what="the job to be claimed")
        self.wait_for(self.starts, what="the job to start")
        subprocess.run([str(FAKE), "kill", owner], env=self.env, check=True, capture_output=True)
        self.finish(p, "e1")
        job = self.exported("e1")["jobs"]["sk-e1-j001"]
        self.assertEqual((job["state"], job["attempt"]), ("done", 2))
        self.assertNotEqual(job["owner"], owner)
        self.assertEqual(len(self.starts()), 2)
        self.assertIn("requeued", [e["event"] for e in self.events("e1")])

    def test_pause_and_resume(self):
        self.board("init", "e1", "--spec", self.spec(["a", "b", "c", "d"], secs=0.4, max_workers=1))
        p = self.start("e1")
        self.wait_for(self.starts, what="the first job")
        self.board("pause", "e1")
        self.wait_for(lambda: not [t for t in self.terminals() if t["state"] == "live"], what="workers to drain")
        seen = len(self.starts())
        self.assertLess(seen, 4)
        time.sleep(1.0)
        self.assertEqual(len(self.starts()), seen)  # nothing is claimed while paused
        self.board("resume", "e1")
        self.finish(p, "e1")
        self.assertIn("COMPLETE done=4 failed=0", self.output("e1"))

    # ---------------------------------------------------------------- preflight

    def test_run_refuses_at_the_start_what_would_only_fail_at_the_end(self):
        self.board("init", "e1", "--spec", self.spec(["A"]))
        plain = Path(self.tmp) / "plain"
        plain.mkdir()
        shutil.copy(self.repo / "job.sh", plain / "job.sh")
        p = self.start("e1", cwd=plain)
        self.finish(p, "e1", rc=2, secs=20)
        self.assertIn("is not a git repo, so the export could not be committed at the end", self.output("e1"))
        self.assertEqual(self.terminals(), [])  # no worker was started
        self.assertEqual(self.status("e1")["manifest"]["state"], "planned")
        self.assertEqual(self.starts(), [])

        (self.repo / ".gitignore").write_text("experiments/\n")
        p = self.start("e1")
        self.finish(p, "e1", rc=2, secs=20)
        self.assertIn("experiments/e1.json is ignored by git", self.output("e1"))
        (self.repo / ".gitignore").unlink()

        (self.repo / "job.sh").chmod(0o644)
        p = self.start("e1")
        self.finish(p, "e1", rc=2, secs=20)
        self.assertIn("job_cmd starts with './job.sh', which is not an executable file", self.output("e1"))
        (self.repo / "job.sh").chmod(0o755)

        self.finish(self.start("e1", "--no-commit", cwd=plain), "e1")  # without the commit a plain folder is fine
        self.assertTrue((plain / "experiments" / "e1.json").is_file())

    # ---------------------------------------------------------------- retry delay

    def test_failed_job_waits_for_its_retry_delay_and_the_worker_stays(self):
        self.board("init", "e1", "--spec", self.spec(["FAIL1"], secs=0, retry_delay_s=1.5, max_workers=1))
        self.assertEqual(self.status("e1")["manifest"]["retry_delay_s"], 1.5)
        p = self.start("e1")
        job = self.wait_for(lambda: next((j for j in (self.status("e1") or {"jobs": {}})["jobs"].values()
                                          if j and j["state"] == "pending" and j["attempt"] == 1), None),
                            what="the first failure")
        self.assertGreaterEqual(job["not_before_ms"] - job["finished_ms"], 1400)
        self.finish(p, "e1")
        d = self.exported("e1")["jobs"]["sk-e1-j001"]
        self.assertEqual((d["state"], d["attempt"]), ("failed", 2))
        self.assertGreaterEqual(d["started_ms"], job["not_before_ms"])  # the retry did not start early
        self.assertEqual(len([e for e in self.events("e1") if e["event"] == "worker_started"]), 1)  # it waited, alive
        self.assertIn("1 job(s) failed. After fixing the cause, rerun only those as a new experiment: "
                      "hcboard.py init <new-id> --retry-failed e1", self.output("e1"))

    # ---------------------------------------------------------------- cancel

    def test_cancel_kills_workers_fails_open_jobs_and_archives(self):
        self.board("init", "e1", "--spec", self.spec(["quick", "SLOW1", "SLOW2", "later"], secs=30))
        spec = json.loads((self.repo / "spec.json").read_text())
        self.assertEqual(spec["max_workers"], 2)
        p = self.start("e1")
        self.wait_for(lambda: len(self.starts()) == 2, what="two jobs running")
        pids = [t["pid"] for t in self.terminals() if t["state"] == "live"]
        out = json.loads(self.board("cancel", "e1").stdout)
        self.assertEqual(out["state"], "cancelled")
        self.finish(p, "e1", secs=30)
        self.assertIn("HCBOARD e1 COMPLETE done=0 failed=4 CANCELLED", self.output("e1"))
        d = self.exported("e1")
        self.assertTrue(d["manifest"]["cancelled"])
        self.assertEqual({j["result"]["error"] for j in d["jobs"].values()}, {"cancelled"})
        self.assertEqual(len(self.starts()), 2)  # nothing new was started
        for pid in pids:  # the workers are gone, and so are their jobs (sleep 30)
            self.wait_for(lambda: not os.path.exists(f"/proc/{pid}"), secs=10, what=f"worker {pid} to exit")
        self.assertEqual(subprocess.run(["pgrep", "-f", "sleep 30"], capture_output=True).stdout, b"")
        self.assertEqual([e["event"] for e in self.events("e1")].count("worker_killed"), 2)
        self.assertEqual(self.terminals(), [])
        p = self.board("cancel", "e1", check=False)  # nothing left to cancel
        self.assertNotEqual(p.returncode, 0)

    def test_cancel_without_an_orchestrator_is_finished_by_the_next_run(self):
        self.board("init", "e1", "--spec", self.spec(["A", "B"]))
        self.board("cancel", "e1")
        self.assertIn("COMPLETE done=0 failed=2 CANCELLED", self.run_board("e1"))
        self.assertEqual(self.starts(), [])

    # ---------------------------------------------------------------- retry failed

    def test_retry_failed_makes_a_child_experiment_of_only_the_failed_jobs(self):
        self.board("init", "e1", "--spec", self.spec(["ok1", "FAIL-a", "ok2", "FAIL-b"], ["v1", "v2"], job_timeout_s=20))
        self.assertIn("COMPLETE done=4 failed=4", self.run_board("e1"))
        res = json.loads(self.board("init", "e2", "--retry-failed", "e1").stdout)
        self.assertEqual(res["jobs"], 4)
        st = self.status("e2")
        m = st["manifest"]
        self.assertEqual((m["parent"], m["delta"], m["job_timeout_s"], m["max_workers"]),
                         ("e1", {"retry_failed": 4}, 20, 2))
        self.assertEqual((m["cases"], m["variants"]), (["FAIL-a", "FAIL-b"], ["v1", "v2"]))
        self.assertEqual(sorted((j["case"], j["variant"], j["attempt"], j["max_attempts"]) for j in st["jobs"].values()),
                         [("FAIL-a", "v1", 0, 2), ("FAIL-a", "v2", 0, 2), ("FAIL-b", "v1", 0, 2), ("FAIL-b", "v2", 0, 2)])
        self.assertEqual(self.exported("e1")["manifest"]["state"], "done")  # the parent's export is untouched

        fixed = self.spec(["x"], name="fixed.json")  # a spec may replace the settings; the pairs still come from e1
        spec = json.loads(Path(fixed).read_text())
        spec["job_cmd"] = ["sh", "-c", "echo '{\"summary\":\"fixed\"}' > {out}"]
        Path(fixed).write_text(json.dumps(spec))
        self.board("init", "e3", "--retry-failed", "e1", "--spec", fixed, "--delta", '{"fix":"new job_cmd"}')
        self.assertIn("COMPLETE done=4 failed=0", self.run_board("e3"))
        self.assertEqual(self.exported("e3")["manifest"]["delta"], {"fix": "new job_cmd"})

    def test_retry_failed_refusals(self):
        p = self.board("init", "e2", "--retry-failed", "e1", check=False)
        self.assertEqual(p.returncode, 2)
        self.assertIn("cannot use", p.stderr)
        self.board("init", "e1", "--spec", self.spec(["A"]))
        self.run_board("e1")
        p = self.board("init", "e2", "--retry-failed", "e1", check=False)
        self.assertIn("e1 has no failed jobs", p.stderr)
        p = self.board("init", "e1", "--retry-failed", "e1", check=False)
        self.assertIn("its own id", p.stderr)
        p = self.board("init", "e2", check=False)
        self.assertIn("needs --spec", p.stderr)

    def test_pairs_run_a_sparse_grid(self):
        spec = {"question": "q", "pairs": [["A", "v1"], ["B", "v2"]], "job_cmd": ["./job.sh", "{case}", "0", "{variant}", "{out}"]}
        (self.repo / "s.json").write_text(json.dumps(spec))
        self.assertEqual(json.loads(self.board("init", "e1", "--spec", "s.json").stdout)["jobs"], 2)
        self.run_board("e1")
        self.assertEqual(sorted(self.starts()), ["A v1", "B v2"])

    # ---------------------------------------------------------------- status

    def test_status_shows_timing_and_variants(self):
        # Nine jobs of 0.4 s on one worker: a few seconds left after the first finishes, so the checks below
        # always see the run mid-way (they used to sit under an `if` that let a finished run pass unchecked).
        cases = ["a", "b", "c", "d", "e", "f", "g", "h", "FAIL"]
        self.board("init", "e1", "--spec", self.spec(cases, ["v1"], secs=0.4, max_workers=1, max_attempts=1))
        p = self.start("e1")
        self.wait_for(lambda: (self.status("e1") or {"counts": {"done": 0}})["counts"]["done"] >= 1, what="one job done")
        st = self.status("e1")
        self.assertIsNotNone(st)
        self.assertGreater(st["counts"]["pending"] + st["counts"]["running"], 0, st["counts"])
        self.assertGreater(st["stats"]["mean_job_s"], 0.3)
        self.assertGreater(st["stats"]["eta_s"], 0)
        self.assertIn("v1", st["variants"])
        text = self.board("status", "e1").stdout
        self.assertRegex(text, r"running for \d+s, 0\.\d+ s per finished job, about \d+s left at 1 worker\(s\) \(rough\)")
        self.assertRegex(text, r"  v1 +done=\d failed=0 open=\d")
        self.finish(p, "e1")

    # ---------------------------------------------------------------- hc row shapes

    def test_slim_ls_rows_are_completed_with_hc_get(self):
        """references/hc-facts.md says `hc ls --json` rows carry no name or exit_code. With such rows the
        orchestrator numbered every later worker <exp>-w1 again, so replacing a dead worker failed on the
        taken name while w1 was alive (and each clean worker exit counted towards the crash limit)."""
        env = {"FAKE_CORE_SLIM": "1"}
        self.board("init", "e1", "--spec", self.spec(["A", "B", "C", "D", "E", "F"], secs=1))
        p = self.start("e1", env=env)
        self.wait_for(lambda: len(self.starts()) >= 2, what="two jobs running")
        w2 = next(t["address"] for t in self.terminals() if t["name"] == "e1-w2")
        subprocess.run([str(FAKE), "kill", w2], env=self.env, check=True, capture_output=True)
        self.finish(p, "e1")
        self.assertIn("COMPLETE done=6 failed=0", self.output("e1"))
        events = self.events("e1")
        self.assertEqual([e["name"] for e in events if e["event"] == "worker_started"][:3], ["e1-w1", "e1-w2", "e1-w3"])
        self.assertNotIn("worker_start_failed", [e["event"] for e in events])
        self.assertEqual(self.terminals(), [])

    # ---------------------------------------------------------------- reads

    def test_finished_jobs_are_not_read_again_on_every_claim_and_event(self):
        n = 30
        self.board("init", "e1", "--spec", self.spec([f"c{i:02d}" for i in range(n)], secs=0, max_workers=1))
        before = self.ops().get("get", 0)
        self.finish(self.start("e1", "--poll", "60"), "e1")
        gets = self.ops()["get"] - before
        # One worker claiming n jobs used to read 1+2+...+n job docs, and every doc event made the
        # orchestrator read all n again: 806 reads for these 30 jobs, growing with n squared. Now 372, of
        # which about 150 are init, the final export and the deletions; the rest is a few reads per job.
        print(f"doc reads for {n} jobs: {gets}", file=sys.stderr) if os.environ.get("HCB_SHOW_READS") else None
        self.assertLess(gets, 16 * n, f"{gets} doc reads for {n} jobs")
        self.assertIn(f"COMPLETE done={n} failed=0", self.output("e1"))

    # ---------------------------------------------------------------- review findings (B1-B11)

    def hcdoc(self, *args):
        subprocess.run([sys.executable, "-I", str(HCDOC), *args], env=self.env, check=True, capture_output=True)

    def test_init_refuses_an_unsafe_out_dir(self):
        """B1: a bad out_dir used to pass init and preflight, then crash every worker after its claim."""
        for bad in ("../x", "/tmp/x", "a/../b"):
            p = self.board("init", "e1", "--spec", self.spec(["A"], out_dir=bad), check=False)
            self.assertEqual(p.returncode, 2, f"{bad}: {p.stdout}{p.stderr}")
            self.assertIn("unsafe out_dir", p.stderr)
        self.assertIsNone(self.status("e1"))

    def test_out_dir_that_cannot_be_created_fails_the_jobs_with_the_reason(self):
        """B1: the worker crashed on makedirs, so each job burnt its attempts as `worker tN died`."""
        (self.repo / "blocked").write_text("a file where the out dir should be\n")
        self.board("init", "e1", "--spec", self.spec(["A", "B"], out_dir="blocked/runs"))
        self.assertIn("COMPLETE done=0 failed=2", self.run_board("e1"))
        for j in self.exported("e1")["jobs"].values():
            self.assertIn("out_dir", j["result"]["error"])
            self.assertEqual(j["attempt"], 2)
        self.assertNotIn("requeued", [e["event"] for e in self.events("e1")])

    def test_workers_that_keep_dying_make_the_run_give_up(self):
        """B2: the requeue that failed a crashed job counted as progress and reset the crash limit."""
        self.board("init", "e1", "--spec", self.spec([f"c{i}" for i in range(6)], max_attempts=3,
                                                    job_cmd=["sh", "-c", "kill -9 $PPID"]))
        self.finish(self.start("e1"), "e1", rc=1)
        self.assertIn("workers died with no job finishing; giving up", self.output("e1"))
        started = [e for e in self.events("e1") if e.get("event") == "worker_started"]  # the last line is the error
        self.assertLessEqual(len(started), 13)  # 10 crashes and the workers that were already starting, not 18

    def test_giving_up_exits_1(self):
        """B3: --timeout, a failed commit, an archived experiment without export and an incomplete export
        exited 2 ("bad input"); the docstring and SKILL.md say 1. Start-up checks stay 2 (tested above)."""
        self.board("init", "e1", "--spec", self.spec(["SLOW"], secs=20, max_workers=1))
        p = self.start("e1", "--timeout", "1")
        self.finish(p, "e1", rc=1, secs=30)
        self.assertIn("--timeout reached", self.output("e1"))
        for t in self.terminals():  # the worker kills its `sleep 20` on SIGHUP
            if t["state"] == "live":
                subprocess.run([str(FAKE), "kill", t["address"]], env=self.env, capture_output=True)

        self.board("init", "e2", "--spec", self.spec(["A"]))
        hook = self.repo / ".git" / "hooks" / "pre-commit"
        hook.parent.mkdir(exist_ok=True)
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        self.finish(self.start("e2"), "e2", rc=1)
        self.assertIn("git commit failed", self.output("e2"))
        self.assertEqual(self.status("e2")["manifest"]["state"], "done")  # docs kept
        hook.unlink()
        self.finish(self.start("e2"), "e2")

        self.board("init", "e3", "--spec", self.spec(["A"]))
        self.hcdoc("update", "sk-e3", "--set-str", "state=archived")
        self.finish(self.start("e3"), "e3", rc=1)
        self.assertIn("is archived but", self.output("e3"))

        self.board("init", "e4", "--spec", self.spec(["A", "B"]))
        self.hcdoc("delete", "sk-e4-j002", "--version", str(self.doc("sk-e4-j002")["version"]))
        self.finish(self.start("e4"), "e4", rc=1)
        self.assertIn("export is incomplete", self.output("e4"))

    def test_init_refuses_the_id_of_an_archived_experiment(self):
        """B4: deleted docs are null at version > 0; init took that for a free id and the new run
        overwrote the old export."""
        self.board("init", "e1", "--spec", self.spec(["A"]))
        self.run_board("e1")
        before = (self.repo / "experiments" / "e1.json").read_text()
        self.assertGreater(self.doc("sk-e1")["version"], 0)
        p = self.board("init", "e1", "--spec", self.spec(["B"]), check=False)
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("pick a new experiment id", p.stderr)
        self.assertIsNone(self.doc("sk-e1")["value"])
        self.assertIsNone(self.doc("sk-e1-j001")["value"])
        self.assertEqual((self.repo / "experiments" / "e1.json").read_text(), before)
        self.board("init", "e2", "--spec", self.spec(["B"]), "--parent", "e1", "--delta", "{}")  # lineage still works
        self.assertEqual(self.status("e2")["manifest"]["parent"], "e1")

    def test_init_that_fails_at_the_manifest_leaves_no_job_docs(self):
        """B6: the manifest was size-checked only after every job doc was written, blocking the id."""
        p = self.board("init", "e1", "--spec", self.spec(["A", "B"], pre={"blob": "x" * 1_100_000}), check=False)
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("bytes (limit", p.stderr)
        self.assertEqual(self.doc("sk-e1-j001"), {"value": None, "version": 0})
        self.board("init", "e1", "--spec", self.spec(["A", "B"]))

    def test_watcher_waits_for_the_core_to_come_back(self):
        """B8: after the watcher's connection dropped, one resubscribe 1 s later was all; if the core was
        not back yet the orchestrator exited 4."""
        self.board("init", "e1", "--spec", self.spec([f"c{i}" for i in range(4)], secs=1, max_workers=1))
        p = self.start("e1")
        self.wait_for(self.starts, what="the first job")

        def watcher():
            r = subprocess.run(["pgrep", "-P", str(p.pid), "-f", "hcdoc.py"], capture_output=True, text=True)
            return r.stdout.split()

        pid = int(self.wait_for(watcher, what="the watcher")[0])
        (self.core / "down").write_text("")  # new connections fail; open ones keep working
        os.kill(pid, 9)
        time.sleep(4)
        self.assertIsNone(p.poll(), self.output("e1")[-2000:])
        (self.core / "down").unlink()
        self.finish(p, "e1")
        self.assertIn("COMPLETE done=4 failed=0", self.output("e1"))
        self.assertIn("watch_restarted", [e["event"] for e in self.events("e1")])


class Units(unittest.TestCase):
    def test_check_spec_pairs(self):
        base = {"question": "q", "job_cmd": ["x"]}
        spec, cases, variants, _, pairs = hcboard.check_spec({**base, "pairs": [["A", "v"], ["B", "w"]]})
        self.assertEqual((cases, variants, pairs), (["A", "B"], ["v", "w"], [("A", "v"), ("B", "w")]))
        for bad in ([], [["A"]], [["A", "v"], ["A", "v"]], [["A", 1]], "A,v"):
            with self.assertRaises(hcdoc.BadInput, msg=str(bad)):
                hcboard.check_spec({**base, "pairs": bad})
        with self.assertRaises(hcdoc.BadInput):
            hcboard.check_spec({**base, "pairs": [["A", "v"]], "cases": ["B"], "variants": ["v"]})
        for key, bad in (("retry_delay_s", -1), ("retry_delay_s", "5"), ("job_timeout_s", 0), ("job_timeout_s", True)):
            with self.assertRaises(hcdoc.BadInput, msg=f"{key}={bad}"):
                hcboard.check_spec({**base, "cases": ["A"], "variants": ["v"], key: bad})

    def test_retry_delay_doubles_and_is_capped(self):
        self.assertEqual([dw.retry_delay_ms(5, a) for a in (1, 2, 3, 4)], [5000, 10000, 20000, 40000])
        self.assertEqual(dw.retry_delay_ms(400, 3), 600000)
        for off in (0, None, -3, "5"):
            self.assertEqual(dw.retry_delay_ms(off, 1), 0)

    def test_finish_ops_without_a_delay_writes_no_not_before(self):
        job = {"schema": 1, "state": "running", "owner": "t1", "attempt": 1, "result": {}}
        v = hcdoc.apply_ops(dict(job), dw.finish_ops("t1", 1, 1, "x", "/none", 3))
        self.assertNotIn("not_before_ms", v)
        v = hcdoc.apply_ops(dict(job), dw.finish_ops("t1", 1, 1, "x", "/none", 3, 2))
        self.assertGreater(v["not_before_ms"], hcdoc.now_ms() + 1500)
        v = hcdoc.apply_ops({**job, "attempt": 3}, dw.finish_ops("t1", 3, 1, "x", "/none", 3, 2))
        self.assertNotIn("not_before_ms", v)  # the last attempt: nothing to wait for

    def test_claim_next_skips_delayed_and_cached_final_jobs(self):
        now = hcdoc.now_ms()
        docs = {"j1": {"state": "done"}, "j2": {"state": "pending", "attempt": 1, "max_attempts": 3,
                                                 "not_before_ms": now + 60000},
                "j3": {"state": "failed"}}
        gets = []

        class S:
            def get(self, n):
                gets.append(n)
                return docs[n], 1

            def update(self, n, mutate):
                return 2, mutate(dict(docs[n]))

        final = set()
        name, job, wait = dw.claim_next(S(), {"jobs": ["j1", "j2", "j3"]}, "t1", final)
        self.assertIsNone(name)
        self.assertTrue(59000 < wait <= 60000)
        self.assertEqual(final, {"j1", "j3"})
        del gets[:]
        docs["j2"]["not_before_ms"] = now - 1
        name, job, wait = dw.claim_next(S(), {"jobs": ["j1", "j2", "j3"]}, "t1", final)
        self.assertEqual((name, job["state"], job["attempt"], wait), ("j2", "running", 2, None))
        self.assertEqual(gets, ["j2"])  # the final ones were not read again
        docs["j2"] = {"state": "pending", "attempt": 3, "max_attempts": 3, "not_before_ms": now + 60000}
        self.assertEqual(dw.claim_next(S(), {"jobs": ["j2"]}, "t1"), (None, None, None))  # exhausted: no reason to wait

    def test_stats(self):
        jobs = {"a": {"state": "done", "started_ms": 1000, "finished_ms": 5000, "attempt": 1},
                "b": {"state": "failed", "started_ms": 2000, "finished_ms": 4000, "attempt": 3},
                "c": {"state": "running", "started_ms": 6000, "attempt": 1},
                "d": {"state": "pending", "attempt": 0}, "e": {"state": "pending"}, "f": None}
        self.assertEqual(hcboard.stats(jobs, 2, 11000), {"elapsed_s": 10.0, "mean_job_s": 3.0, "eta_s": 4.5, "retries": 2})
        self.assertEqual(hcboard.stats({"d": {"state": "pending"}}, 2, 0),
                         {"elapsed_s": None, "mean_job_s": None, "eta_s": None, "retries": 0})
        self.assertEqual(hcboard.stats({"a": jobs["a"]}, 2, 6000)["eta_s"], 0)
        self.assertEqual([hcboard.fmt_secs(s) for s in (5, 65, 3725)], ["5s", "1m05s", "1h02m"])

    def orchestrator(self, docs):
        """An Orchestrator over an in-memory doc store that records which docs are read."""
        import argparse
        o = hcboard.Orchestrator(argparse.Namespace(exp="e1", cwd=".", poll=30, no_commit=True, timeout=None))
        reads = []

        class S:
            def get(self, n):
                reads.append(n)
                return docs.get(n), 1

            def update(self, n, mutate):
                docs[n] = mutate(dict(docs[n]))
                return 2, docs[n]

            def close(self):
                pass

        o.sess = S()
        o.workers = lambda: []
        o.start_worker = lambda terms: False
        return o, reads

    def test_orchestrator_rereads_only_docs_with_events_until_something_forces_a_full_read(self):
        names = [f"sk-e1-j00{i}" for i in (1, 2, 3)]
        docs = {"sk-e1": {"schema": 1, "state": "running", "jobs": names, "max_workers": 1},
                **{n: {"schema": 1, "state": "pending", "attempt": 0, "max_attempts": 2} for n in names}}
        o, reads = self.orchestrator(docs)
        o.read_jobs()
        self.assertEqual(reads, ["sk-e1"] + names)  # the first read is complete
        del reads[:]
        o.read_jobs()
        self.assertEqual(reads, ["sk-e1"])  # no events: only the manifest
        docs[names[1]]["state"] = "done"
        o.on_message({"kind": "doc", "name": names[1]})
        o.on_message({"kind": "doc", "name": "sk-other-j001"})  # another experiment's doc
        del reads[:]
        _, jobs = o.read_jobs()
        self.assertEqual(reads, ["sk-e1", names[1]])
        self.assertEqual(jobs[names[1]]["state"], "done")
        for force in (lambda: o.on_message({"kind": "snapshot", "reason": "missed"}),
                      lambda: docs["sk-e1"].update(jobs=names[:2])):
            force()
            del reads[:]
            o.read_jobs()
            self.assertEqual(reads[0], "sk-e1")
            self.assertGreaterEqual(len(reads), 3, reads)  # every job doc again

    def test_all_final_is_confirmed_with_a_full_read_before_finishing(self):
        names = ["sk-e1-j001", "sk-e1-j002"]
        docs = {"sk-e1": {"schema": 1, "state": "running", "jobs": names, "max_workers": 1},
                **{n: {"schema": 1, "state": "done", "attempt": 1, "max_attempts": 2} for n in names}}
        o, reads = self.orchestrator(docs)
        self.assertTrue(o.reconcile())
        # A job went back to pending and the event for it never arrived: the cache still says "all done".
        docs[names[0]] = {"schema": 1, "state": "pending", "attempt": 1, "max_attempts": 2}
        del reads[:]
        self.assertFalse(o.reconcile())
        self.assertEqual(sorted(set(reads)), ["sk-e1"] + names)

    def test_timeout_kill_survives_a_job_that_exited_just_before_it(self):
        """B10: wait(timeout) expired, the job's group exited, then os.killpg raised ProcessLookupError."""
        tmp = tempfile.mkdtemp(prefix="hcb")
        self.addCleanup(shutil.rmtree, tmp, True)
        real = os.killpg

        def racy(pid, sig):
            real(pid, sig)  # the group is gone by the time the worker's own kill arrives
            raise ProcessLookupError(3, "No such process")

        with mock.patch.object(dw.os, "killpg", racy):
            rc, err = dw.run_command(["sleep", "5"], tmp, os.path.join(tmp, "log"), 0.2)
        self.assertEqual((rc, err), (None, "timed out after 0.2s"))

    def test_odd_or_hung_hc_does_not_crash_the_orchestrator(self):
        """B11: non-JSON `hc run` output or a timed-out hc raised out of reconcile."""
        o, _ = self.orchestrator({})
        del o.start_worker  # the real one
        out = io.StringIO()

        def hung(argv, **kw):
            raise subprocess.TimeoutExpired(argv, 60)

        with contextlib.redirect_stdout(out):
            with mock.patch.object(hcboard.subprocess, "run",
                                   return_value=subprocess.CompletedProcess([], 0, "not json\n", "")):
                self.assertFalse(o.start_worker([]))
            with mock.patch.object(hcboard.subprocess, "run", hung):
                self.assertFalse(o.start_worker([]))
                self.assertTrue(o.cancel([{"state": "live", "address": "t1", "name": "e1-w1"}], {}))
                o.workers = lambda: [{"state": "finished", "address": "t2"}]
                o.remove_finished_workers()
        events = [json.loads(line)["event"] for line in out.getvalue().splitlines()]
        self.assertEqual(events.count("worker_start_failed"), 2, events)
        self.assertEqual(events.count("warning"), 2, events)

    def test_cancel_mutation_leaves_finished_jobs_alone(self):
        v = hcboard.cancel_mutation({"state": "running", "owner": "t1", "result": {"error": None}})
        self.assertEqual((v["state"], v["result"]["error"]), ("failed", "cancelled"))
        for state in ("done", "failed"):
            with self.assertRaises(hcdoc.Lost):
                hcboard.cancel_mutation({"state": state})


if __name__ == "__main__":
    unittest.main()
