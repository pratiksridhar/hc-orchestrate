"""Offline tests for scripts/hcboard.py and scripts/docstore_worker.py (pure logic, no hc needed)."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import hcboard  # noqa: E402
import hcdoc  # noqa: E402
import docstore_worker as dw  # noqa: E402


def write(path, text):
    with open(path, "w") as f:
        f.write(text)


def job(state="pending", owner=None, attempt=0, mx=3, **kw):
    return {"schema": 1, "case": "C", "variant": "v", "state": state, "owner": owner, "attempt": attempt,
            "max_attempts": mx, "result": {"out": None, "summary": None, "error": None}, **kw}


class Planning(unittest.TestCase):
    def test_job_names_fit_the_32_char_limit(self):
        names = hcboard.job_names("e" + "x" * 15, 999)
        self.assertEqual(names[0], "sk-exxxxxxxxxxxxxxx-j001")
        self.assertEqual(names[-1], "sk-exxxxxxxxxxxxxxx-j999")
        for n in names:
            hcdoc.check_name(n)

    def test_exp_ids(self):
        for ok in ("e051", "e0", "a_b", "9"):
            hcboard.check_exp(ok)
        for bad in ("", "E1", "e-1", "e.1", "x" * 17, "_a", "e 1", "../x"):
            with self.assertRaises(hcdoc.BadInput, msg=bad):
                hcboard.check_exp(bad)

    def test_counts(self):
        c = hcboard.counts({"a": job(), "b": job("running"), "c": job("done"), "d": job("failed"), "e": None,
                            "f": job("weird")})
        self.assertEqual(c, {"pending": 1, "running": 1, "done": 1, "failed": 1, "missing": 2})

    def test_orphans_are_running_jobs_with_dead_owners_only(self):
        jobs = {"a": job("running", "t1"), "b": job("running", "t2"), "c": job("pending"), "d": job("done", "t3"),
                "e": job("running", "m/t9"), "f": job("running", None), "g": None}
        self.assertEqual([n for n, _ in hcboard.orphans(jobs, {"t1"})], ["b", "f"])
        self.assertEqual(hcboard.orphans(jobs, {"t1", "t2"}), [("f", jobs["f"])])

    def test_exhausted(self):
        jobs = {"a": job(attempt=3, mx=3), "b": job(attempt=2, mx=3), "c": job("running", "t1", attempt=3, mx=3)}
        self.assertEqual([n for n, _ in hcboard.exhausted(jobs)], ["a"])

    def test_workers_to_start(self):
        f = hcboard.workers_to_start
        self.assertEqual(f(3, 12, 0, 0), 3)   # fresh start
        self.assertEqual(f(3, 9, 3, 3), 0)    # steady state
        self.assertEqual(f(3, 9, 2, 2), 1)    # one worker died
        self.assertEqual(f(3, 1, 0, 0), 1)    # fewer jobs than slots
        self.assertEqual(f(3, 0, 2, 3), 0)    # tail: nothing left to take
        self.assertEqual(f(3, 0, 0, 0), 0)
        self.assertEqual(f(2, 50, 0, 5), 0)   # never negative

    def test_requeue_ops(self):
        v = hcdoc.apply_ops(job("running", "t4", 1), hcboard.requeue_ops("t4", 1, 3, "worker t4 died"))
        self.assertEqual((v["state"], v["owner"], v["attempt"], v["result"]["error"]), ("pending", None, 1, "worker t4 died"))
        v = hcdoc.apply_ops(job("running", "t4", 3), hcboard.requeue_ops("t4", 3, 3, "worker t4 died"))
        self.assertEqual((v["state"], v["attempt"]), ("failed", 3))
        self.assertIsInstance(v["finished_ms"], int)

    def test_requeue_does_not_clobber_a_job_that_moved_on(self):
        for moved in (job("done", "t4", 1), job("running", "t5", 1), job("running", "t4", 2)):
            with self.assertRaises(hcdoc.Lost):
                hcdoc.apply_ops(moved, hcboard.requeue_ops("t4", 1, 3, "x"))

    def test_table_and_variants(self):
        jobs = {"sk-e-j001": job("done", case="A", variant="v1", attempt=1,
                                 result={"error": None, "summary": "ok", "out": "p"}),
                "sk-e-j002": job("failed", case="B", variant="v1", attempt=3,
                                 result={"error": "exit 1: boom", "summary": None, "out": None})}
        t = hcboard.table(jobs)
        self.assertIn("exit 1: boom", t)
        self.assertIn("3/3", t)
        self.assertEqual(hcboard.by_variant(jobs), {"v1": {"done": 1, "failed": 1, "other": 0}})


class Spec(unittest.TestCase):
    def spec(self, **over):
        d = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, d, True)
        s = {"question": "q", "cases": ["A"], "variants": ["v"], "job_cmd": ["./j.sh", "{case}", "{out}"], **over}
        p = os.path.join(d, "spec.json")
        write(p, json.dumps(s))
        return p

    def test_defaults(self):
        spec, cases, variants, cmd, pairs = hcboard.read_spec(self.spec())
        self.assertEqual(pairs, [("A", "v")])
        self.assertEqual((spec["max_workers"], spec["max_attempts"], spec["pre"]), (3, 3, {}))

    def test_rejects_bad_specs(self):
        for over in ({"cases": []}, {"cases": "A"}, {"variants": [1]}, {"job_cmd": []}, {"max_workers": 0},
                     {"max_workers": 99}, {"max_attempts": "3"}, {"max_attempts": True}, {"question": None},
                     {"pre": []}, {"cases": [""]}):
            with self.assertRaises(hcdoc.BadInput, msg=str(over)):
                hcboard.read_spec(self.spec(**over))

    def test_refuses_zowe_anywhere_in_job_cmd(self):
        for cmd in (["zowe", "files", "ls"], ["sh", "-c", "ZOWE jobs list"], ["./run.sh", "x/zowe"], ["env", "zowe"]):
            with self.assertRaises(hcdoc.BadInput, msg=str(cmd)):
                hcboard.read_spec(self.spec(job_cmd=cmd))

    def test_missing_file(self):
        with self.assertRaises(hcdoc.BadInput):
            hcboard.read_spec("/nonexistent/spec.json")


class WorkerLogic(unittest.TestCase):
    def test_fill_substitutes_once_and_only_known_names(self):
        vals = {"case": "{variant}", "variant": "V", "out": "o.json", "job": "j", "exp": "e", "attempt": 2}
        self.assertEqual(dw.fill(["x{case}y", "{variant}", "{unknown}", "{attempt}"], vals),
                         ["x{variant}y", "V", "{unknown}", "2"])

    def test_out_dir_must_be_relative_and_clean(self):
        self.assertEqual(dw.out_dir_for({"exp": "e1"}), "runs/e1")
        self.assertEqual(dw.out_dir_for({"exp": "e1", "out_dir": "a/b"}), "a/b")
        self.assertEqual(dw.out_dir_for({"exp": "e1", "out_dir": ""}), "runs/e1")  # empty means default
        for bad in ("/etc", "../x", "a/../b", "a b", "~/x", "-x"):
            with self.assertRaises(hcdoc.BadInput, msg=bad):
                dw.out_dir_for({"exp": "e1", "out_dir": bad})

    def test_finish_ops_success(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "o.json")
            write(out, json.dumps({"ok": True}))
            v = hcdoc.apply_ops(job("running", "t1", 1), dw.finish_ops("t1", 1, 0, None, out, 3))
        self.assertEqual((v["state"], v["result"]["out"], v["result"]["error"]), ("done", out, None))
        self.assertEqual(v["result"]["summary"], '{"ok":true}')

    def test_finish_ops_failure_retries_until_max_attempts(self):
        v = hcdoc.apply_ops(job("running", "t1", 1), dw.finish_ops("t1", 1, 1, "exit 1: x", "/none", 3))
        self.assertEqual((v["state"], v["owner"], v["result"]["error"]), ("pending", None, "exit 1: x"))
        v = hcdoc.apply_ops(job("running", "t1", 3), dw.finish_ops("t1", 3, 1, "exit 1: x", "/none", 3))
        self.assertEqual((v["state"], v["owner"]), ("failed", "t1"))

    def test_zombie_cannot_overwrite_a_newer_attempt(self):
        stale = dw.finish_ops("t1", 1, 0, None, "/none", 3)
        for current in (job("running", "t2", 2), job("running", "t1", 2), job("pending", None, 1), job("failed", "t1", 1)):
            with self.assertRaises(hcdoc.Lost):
                hcdoc.apply_ops(current, stale)

    def test_summarize(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "o.json")
            write(p, json.dumps({"summary": "found 2 bugs", "big": 1}))
            self.assertEqual(dw.summarize(p), "found 2 bugs")
            write(p, "not json")
            self.assertIsNone(dw.summarize(p))
            write(p, json.dumps({"x": "a" * 5000}))
            self.assertIsNone(dw.summarize(p))
            self.assertIsNone(dw.summarize(os.path.join(d, "missing.json")))
            write(p, json.dumps({"summary": "s" * 1000}))
            self.assertEqual(len(dw.summarize(p)), dw.SUMMARY_MAX)

    def test_run_command_captures_output_and_exit_code(self):
        with tempfile.TemporaryDirectory() as d:
            log = os.path.join(d, "l.log")
            self.assertEqual(dw.run_command(["sh", "-c", "echo hi; exit 3"], d, log, None), (3, None))
            with open(log) as f:
                self.assertEqual(f.read().strip(), "hi")
            rc, err = dw.run_command(["sleep", "5"], d, log, 0.3)
            self.assertIsNone(rc)
            self.assertIn("timed out", err)
            rc, err = dw.run_command(["/nonexistent/x"], d, log, None)
            self.assertEqual(rc, 127)

    def test_worker_needs_hc_terminal_id(self):
        old = os.environ.pop("HC_TERMINAL_ID", None)
        try:
            self.assertEqual(dw.main(["e1"]), hcdoc.BAD)
        finally:
            if old:
                os.environ["HC_TERMINAL_ID"] = old


class SessionTests(unittest.TestCase):
    def test_reopens_once_then_gives_up(self):
        opened = []

        class C:
            def close(self):
                pass

        def fake_open():
            opened.append(1)
            return C()

        s = hcdoc.Session()
        old_open, old_sleep = hcdoc.Conn.open, hcdoc.sleep
        hcdoc.Conn.open, hcdoc.sleep = staticmethod(fake_open), lambda _: None
        try:
            calls = []

            def flaky(c):
                calls.append(1)
                if len(calls) == 1:
                    raise hcdoc.Down("dropped")
                return "ok"

            self.assertEqual(s._do(flaky), "ok")
            self.assertEqual(len(opened), 2)

            def dead(c):
                raise hcdoc.Down("still down")

            with self.assertRaises(hcdoc.Down):
                s._do(dead)
        finally:
            hcdoc.Conn.open, hcdoc.sleep = old_open, old_sleep


if __name__ == "__main__":
    unittest.main()
