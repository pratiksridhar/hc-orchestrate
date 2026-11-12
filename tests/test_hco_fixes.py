"""Regression tests for defects found by reading scripts/hco.py (run cl4u reviews rev-hco and rev-team).
Each test failed before its fix. Run: python3 -I -m unittest discover -s tests -p "test_hco*.py"

Same setup as test_hco.py: a throwaway git repo, tests/fake_hc.py and (team tests) tests/fake_core.py.
"""
import argparse
import io
import json
import os
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from test_hco import FAKE, hco
from test_hco_flow import FlowBase
from test_hcteam import TeamBase


class Fixes(FlowBase):
    def rundir(self, run):
        return self.repo / ".hc-orchestrate" / run

    def inproc(self, *argv):
        """hco.main(argv) in this process, with the test's fake hc and environment: (code, stdout, stderr)."""
        env = {k: self.env[k] for k in ("HCO_HC", "FAKE_HC_STATE", "HC_TERMINAL_ID", "XDG_CONFIG_HOME",
                                        "FAKE_HC_TEMPLATES")}
        out, err, old = io.StringIO(), io.StringIO(), os.getcwd()
        os.chdir(self.repo)
        try:
            with mock.patch.dict(os.environ, env), mock.patch.object(hco, "HC", str(FAKE)), \
                    redirect_stdout(out), redirect_stderr(err):
                code = hco.main(list(argv))
        finally:
            os.chdir(old)
        return code, out.getvalue(), err.getvalue()

    # A1
    def test_parallel_trust_keeps_every_flag(self):
        """Six `--trust` edits at once: each read-modify-write must see the others' flags."""
        import tomllib
        cj, cc = Path(self.tmp.name) / "claude.json", Path(self.tmp.name) / "codex" / "config.toml"
        real, errors = hco._write_atomic, []

        def slow(path, text):  # widen the window between read and write
            time.sleep(0.05)
            real(path, text)

        def one(agent, cwd):
            try:
                hco.trust_folder(agent, cwd)
            except Exception as e:  # noqa: BLE001 - reported below
                errors.append(e)

        with mock.patch.dict(os.environ, {"HCO_CLAUDE_JSON": str(cj), "HCO_CODEX_CONFIG": str(cc)}), \
                mock.patch.object(hco, "_write_atomic", slow):
            for agent in ("claude", "codex"):
                ts = [threading.Thread(target=one, args=(agent, f"/w/{i}")) for i in range(6)]
                for t in ts:
                    t.start()
                for t in ts:
                    t.join()
        self.assertEqual(errors, [])
        projects = json.loads(cj.read_text())["projects"]
        self.assertEqual(sorted(projects), [f"/w/{i}" for i in range(6)])
        self.assertTrue(all(p["hasTrustDialogAccepted"] for p in projects.values()))
        self.assertEqual(sorted(tomllib.loads(cc.read_text())["projects"]), [f"/w/{i}" for i in range(6)])

    # A2 (reduced)
    def test_spawn_waits_for_hc_run_less_than_the_bash_tool_limit(self):
        run = self.init_run()
        calls, real = [], hco.hc_json

        def spy(args, *rest, **kw):
            calls.append((args[0], kw.get("timeout")))
            return real(args, *rest, **kw)

        with mock.patch.object(hco, "hc_json", spy):
            code, out, err = self.inproc("spawn", "--run", run, "--name", "w1", "--agent", "claude",
                                         "--brief", self.brief())
        self.assertEqual(code, 0, err)
        timeouts = [t for cmd, t in calls if cmd == "run"]
        self.assertEqual(len(timeouts), 1)
        self.assertLess(timeouts[0], 120)

    # A3
    def test_merge_failure_that_is_not_a_conflict_says_why(self):
        run = self.init_run()
        _, wt, _ = self.writer(run)
        (wt / "a.txt").write_text("worker\n")
        (self.repo / "a.txt").write_text("the lead's untracked file\n")
        p = self.hco("merge", "--run", run, check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("would be overwritten", p.stdout)
        self.assertNotIn("CONFLICT", p.stdout)
        self.assertEqual((self.repo / "a.txt").read_text(), "the lead's untracked file\n")
        self.assertEqual([e["event"] for e in map(json.loads, (self.rundir(run) / "events.jsonl").read_text()
                                                  .splitlines())][-1], "merge_failed")

    # A4
    def test_accept_refuses_a_scope_it_could_not_check(self):
        run = self.init_run()
        out = json.loads(self.spawn(run, "w1", "--scope", "src/**").stdout)
        self.set_worker(run, "w1", snapshot=None)  # e.g. the snapshot failed at spawn
        self.report(Path(out["brief"]).parent)
        p = self.hco("accept", "--run", run, "--name", "w1", check=False)
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("scope", p.stdout)
        self.hco("accept", "--run", run, "--name", "w1", "--force")

    # A5
    def test_scope_add_keeps_a_pattern_added_meanwhile(self):
        run = self.init_run()
        self.writer(run, "w1", "--scope", "src/**")
        stale = (self.rundir(run), self.run_json(run))  # what a slow `scope --add` read before the other one
        self.hco("scope", "--run", run, "--name", "w1", "--add", "docs/**")
        with mock.patch.object(hco, "load_run", lambda r: stale), redirect_stdout(io.StringIO()):
            hco.cmd_scope(argparse.Namespace(run=run, name="w1", add=["tests/**"]))
        self.assertEqual(self.run_json(run)["workers"]["w1"]["scope"], ["src/**", "docs/**", "tests/**"])

    # A6
    def test_check_that_cannot_start_leaves_the_running_checks_log_alone(self):
        run = self.init_run()
        self.writer(run, "w1", "--check", "true")
        log = self.rundir(run) / "checks" / "w1.log"
        log.parent.mkdir()
        log.write_text("output of the check that is running\n")  # another `hco check` started it
        p = self.hco("check", "--run", run, "--name", "w1", env={"FAKE_HC_RUN_FAIL": "1"}, check=False)
        self.assertEqual(p.returncode, 1)
        self.assertEqual(log.read_text(), "output of the check that is running\n")
        self.assertIn("PASSED", self.hco("check", "--run", run, "--name", "w1").stdout)  # a normal check still logs
        self.assertTrue(log.is_file())

    # A7
    def test_worker_name_that_matches_a_check_terminal_is_refused(self):
        run = self.init_run()
        self.spawn(run, "api")
        p = self.spawn(run, "c-api", check=False)  # its terminal would be api's check terminal
        self.assertEqual(p.returncode, 1)
        self.assertIn("check terminal", p.stderr)
        self.spawn(run, "c-x")
        p = self.spawn(run, "x", check=False)  # x's check terminal would be c-x's terminal
        self.assertEqual(p.returncode, 1)
        self.assertIn("check terminal", p.stderr)
        self.assertEqual(sorted(self.run_json(run)["workers"]), ["api", "c-x"])
        brief = Path(self.brief())
        plan = brief.parent / "plan.json"
        plan.write_text(json.dumps({"workers": [
            {"name": "ui", "agent": "claude", "role": "review", "brief": brief.name},
            {"name": "c-ui", "agent": "claude", "role": "review", "brief": brief.name},
            {"name": "c-api", "agent": "claude", "role": "review", "brief": brief.name}]}))
        p = self.hco("plan", "--run", run, "--file", str(plan), check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("c-ui", p.stderr)
        self.assertIn("c-api", p.stderr)

    # A8
    def test_merge_refuses_a_follow_up_round_that_was_not_verified(self):
        run = self.init_run()
        _, wt, ob = self.writer(run)  # no check
        (wt / "f.txt").write_text("v1\n")
        self.report(ob)
        self.hco("accept", "--run", run, "--name", "w1")
        self.hco("send", "--run", run, "--name", "w1", "--text", "also do y")
        (wt / "f.txt").write_text("half done\n")  # the worker is mid-edit
        p = self.hco("merge", "--run", run, check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("NOT VERIFIED w1", p.stdout)
        self.report(ob)  # DONE for round 2, but not accepted again
        p = self.hco("merge", "--run", run, check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("NOT VERIFIED w1", p.stdout)
        self.hco("merge", "--run", run, "--dry-run")
        self.hco("accept", "--run", run, "--name", "w1")
        self.assertIn("merged w1", self.hco("merge", "--run", run).stdout)

    def test_follow_up_round_with_a_passed_check_merges_without_a_new_accept(self):
        run = self.init_run()
        _, wt, ob = self.writer(run, "w1", "--check", "true")
        (wt / "f.txt").write_text("v1\n")
        self.report(ob)
        self.hco("check", "--run", run, "--name", "w1")
        self.hco("accept", "--run", run, "--name", "w1")
        self.hco("send", "--run", run, "--name", "w1", "--text", "also do y")
        (wt / "f.txt").write_text("v2\n")
        p = self.hco("merge", "--run", run, check=False)  # check stale, no DONE for round 2
        self.assertEqual(p.returncode, 1)
        self.report(ob)
        self.hco("check", "--run", run, "--name", "w1")  # passed on the current files; not accepted again
        self.assertIn("merged w1", self.hco("merge", "--run", run).stdout)

    def test_skip_checks_merges_an_unverified_follow_up_round(self):
        run = self.init_run()
        _, wt, ob = self.writer(run)
        (wt / "f.txt").write_text("v1\n")
        self.report(ob)
        self.hco("send", "--run", run, "--name", "w1", "--text", "also do y")
        p = self.hco("merge", "--run", run, "--skip-checks")
        self.assertIn("merged w1", p.stdout)

    # A9
    def test_dependency_on_a_worker_spawned_after_the_status_read(self):
        run = self.init_run()
        out = json.loads(self.spawn(run, "x").stdout)
        self.report(Path(out["brief"]).parent)
        self.assertIsNone(hco.dep_unmet(self.run_json(run), "x:done", {}))

    # A10
    def test_relative_plan_cwd_is_relative_to_the_plan_file(self):
        run = self.init_run()
        (self.repo / "sub").mkdir()
        plans = self.repo / "plans"
        plans.mkdir()
        (plans / "t.md").write_text("## Goal\nLook.\n")
        (plans / "plan.json").write_text(json.dumps({"workers": [
            {"name": "w1", "agent": "claude", "role": "review", "brief": "t.md", "cwd": "../sub"}]}))
        self.hco("plan", "--run", run, "--file", str(plans / "plan.json"))
        p = self.hco("advance", "--run", run, check=False)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.run_json(run)["workers"]["w1"]["cwd"], str((self.repo / "sub").resolve()))

    # A11
    def test_missing_brief_or_followup_file_is_a_plain_error(self):
        run = self.init_run()
        p = self.hco("spawn", "--run", run, "--name", "w1", "--agent", "claude", "--brief", "/nonexistent/b.md",
                     check=False)
        self.assertEqual(p.returncode, 2)
        self.assertTrue(p.stderr.startswith("hco: "), p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertEqual(self.run_json(run)["workers"], {})
        out = json.loads(self.spawn(run, "w1").stdout)
        self.report(Path(out["brief"]).parent)
        p = self.hco("send", "--run", run, "--name", "w1", "--file", "/nonexistent/f.md", check=False)
        self.assertEqual(p.returncode, 2)
        self.assertTrue(p.stderr.startswith("hco: "), p.stderr)
        self.assertNotIn("Traceback", p.stderr)

    # A12
    def test_report_does_not_call_a_stale_check_passed(self):
        run = self.init_run()
        _, wt, ob = self.writer(run, "w1", "--check", "true")
        self.report(ob)
        self.hco("check", "--run", run, "--name", "w1")
        row = lambda: next(l for l in self.hco("report", "--run", run).stdout.splitlines() if l.startswith("| w1 "))
        self.assertIn("| passed (", row())
        (wt / "late.txt").write_text("x\n")
        self.assertIn("then files changed", row())


class TeamFixes(TeamBase):
    def add_file(self, entries):
        f = Path(self.tmp.name) / "tasks.json"
        f.write_text(json.dumps(entries))
        return self.hco("task", "add", "--run", self.run_id, "--file", str(f), check=False)

    def task_list(self):
        return json.loads(self.hco("tasks", "--run", self.run_id, "--json").stdout)

    # T2 + T3
    def test_task_file_with_a_bad_entry_adds_nothing(self):
        self.team("api")
        cases = {
            2: [{"title": "a"}, {"title": "b", "for": "typo"}],
            2.1: [{"title": "a"}, {"title": "x" * 121}],
            2.2: [{"title": "a"}, {"title": "b", "after": [9]}],
            2.3: [{"title": "a"}, {"title": "b", "after": 3}],
            2.4: [{"title": "a", "key": "model"}, {"title": "b", "after": "model"}],
            1: [{"title": "a", "key": ["x"]}],
            1.1: [{"title": "a", "key": 1}, {"title": "b", "after": [1]}],
            2.5: [{"title": "a"}, {"title": "b", "for": 3}],
            2.6: [{"title": "a"}, {"title": "b", "detail": 5}],
            2.7: [{"title": "a"}, {"title": "b", "detail": "d" * 2001}],
            2.8: [{"title": "a"}, {"title": "b", "after": [True]}],
            101: [{"title": f"t{i}"} for i in range(101)],
        }
        for entry, entries in cases.items():
            with self.subTest(entries=json.dumps(entries)[:70]):
                p = self.add_file(entries)
                self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
                self.assertIn(f"entry {int(entry)}", p.stderr)
                self.assertNotIn("Traceback", p.stderr)
                self.assertEqual(self.task_list(), [])
        p = self.add_file([{"title": "a", "key": "k"}, {"title": "b", "after": ["k"], "for": "api", "detail": "d"}])
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual([(t["n"], t["after"], t["assignee"]) for t in self.task_list()], [(1, [], None), (2, [1], "api")])
        self.assertEqual(self.add_file([{"title": "c", "after": [2]}]).returncode, 0)

    # T4
    def test_cancel_tells_idle_members_about_tasks_it_unblocked(self):
        self.team("api", "ui")
        self.add("first")
        self.add("second", "--after", "1")
        self.w("api", "claim", "1")
        f = Path(self.env["FAKE_HC_STATE"])
        st = json.loads(f.read_text())
        st["sent"] = []
        f.write_text(json.dumps(st))
        self.hco("task", "cancel", "--run", self.run_id, "--id", "1")
        self.assertTrue(any(addr == self.addr["ui"] and "claim" in text for addr, text in self.sent()), self.sent())

    # T13
    def test_closed_team_without_its_archive_says_so(self):
        run = self.team("api")
        self.add("first")
        self.hco("cleanup", "--run", run)
        arch = self.repo / ".hc-orchestrate" / run / "team.json"
        for broken in (None, "not json"):
            if broken is None:
                arch.unlink()
            else:
                arch.write_text(broken)
            for cmd in ("tasks", "mail"):
                with self.subTest(cmd=cmd, broken=broken):
                    p = self.hco(cmd, "--run", run, check=False)
                    self.assertNotEqual(p.returncode, 0, p.stdout)
                    self.assertIn("team.json", p.stderr)
                    self.assertNotIn("Traceback", p.stderr)
            self.assertIn("team.json", self.hco("collect", "--run", run).stdout)  # collect still shows the rest


if __name__ == "__main__":
    unittest.main()
