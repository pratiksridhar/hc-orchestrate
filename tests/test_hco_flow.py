"""Tests for hco's run state, checks, accept, follow-up rounds, plans and restart recovery.
Run: python3 -I -m unittest discover -s tests -v

Same setup as test_hco.py: a throwaway git repo and tests/fake_hc.py; nothing touches a real hc.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import gitenv  # noqa: F401  (git without the developer's global config)

from test_hco import HCO, Base, hco


class FlowBase(Base):
    def run_json(self, run):
        return json.loads((self.repo / ".hc-orchestrate" / run / "run.json").read_text())

    def set_worker(self, run, name, **fields):
        f = self.repo / ".hc-orchestrate" / run / "run.json"
        data = json.loads(f.read_text())
        data["workers"][name].update(fields)
        f.write_text(json.dumps(data))

    def fake_state(self):
        return json.loads(Path(self.env["FAKE_HC_STATE"]).read_text())

    def set_terminal(self, name, **fields):
        f = Path(self.env["FAKE_HC_STATE"])
        s = json.loads(f.read_text())
        for t in s["terminals"].values():
            if t["name"] == name:
                t.update(fields)
        f.write_text(json.dumps(s))

    def writer(self, run, name="w1", *extra, **kw):
        """A worktree writer; returns (spawn output, worktree path, outbox path)."""
        out = json.loads(self.spawn(run, name, "--worktree", *extra, **kw).stdout)
        return out, Path(out["cwd"]), Path(out["brief"]).parent

    def report(self, outbox, status="DONE", result="# done\n"):
        (outbox / "STATUS").write_text(status + "\n")
        (outbox / "RESULT.md").write_text(result)

    def row(self, run, name="w1", **kw):
        rows = json.loads(self.hco("status", "--run", run, "--json", **kw).stdout)
        return next(r for r in rows if r["worker"] == name)


class RunState(FlowBase):
    def test_spawns_at_the_same_time_all_land_in_run_json(self):
        """Regression: spawn read run.json, started the terminal, then wrote run.json back, so of several
        spawns running together only the last one's worker survived."""
        run = self.init_run()
        brief = self.brief()
        env = {**self.env, "FAKE_HC_RUN_DELAY": "0.4"}
        procs = [subprocess.Popen([sys.executable, "-I", str(HCO), "spawn", "--run", run, "--name", f"w{i}",
                                   "--agent", "claude", "--brief", brief, "--worktree"],
                                  cwd=self.repo, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for i in range(6)]
        for p in procs:
            out, err = p.communicate(timeout=60)
            self.assertEqual(p.returncode, 0, err.decode())
        data = self.run_json(run)
        self.assertEqual(sorted(data["workers"]), [f"w{i}" for i in range(6)])
        self.assertEqual(data["starting"], {})

    def test_failed_spawn_removes_its_worktree_and_frees_the_name(self):
        run = self.init_run()
        p = self.spawn(run, "w1", "--worktree", env={"FAKE_HC_RUN_FAIL": "1"}, check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("spawn failed", p.stderr)
        self.assertFalse((self.repo / ".hc-orchestrate" / run / "wt" / "w1").exists())
        branches = subprocess.run(["git", "branch", "--list", "hco/*"], cwd=self.repo, capture_output=True,
                                  text=True).stdout
        self.assertEqual(branches.strip(), "")
        self.assertEqual(self.run_json(run)["workers"], {})
        self.spawn(run, "w1", "--worktree")  # the same name works again
        self.assertIn("w1", self.run_json(run)["workers"])

    def test_failed_spawn_in_a_shared_checkout_removes_only_its_outbox(self):
        run = self.init_run()
        (self.repo / "keep.txt").write_text("k\n")
        self.spawn(run, "w1", env={"FAKE_HC_RUN_FAIL": "1"}, check=False)
        self.assertTrue((self.repo / "keep.txt").is_file())
        self.assertFalse((self.repo / ".hc-orchestrate" / run / "out" / "w1").exists())

    def test_a_name_being_spawned_is_reserved(self):
        run = self.init_run()
        f = self.repo / ".hc-orchestrate" / run / "run.json"
        data = json.loads(f.read_text())
        data["starting"] = {"w1": time.time()}
        f.write_text(json.dumps(data))
        p = self.spawn(run, "w1", check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("in progress", p.stderr)
        data["starting"] = {"w1": time.time() - 3600}  # a spawn that died long ago no longer blocks
        f.write_text(json.dumps(data))
        self.spawn(run, "w1")

    def test_leftover_worktree_from_a_dead_spawn_is_not_reused_silently(self):
        run = self.init_run()
        wt = self.repo / ".hc-orchestrate" / run / "wt" / "w1"
        subprocess.run(["git", "worktree", "add", "-q", "-b", f"hco/{run}/w1", str(wt)], cwd=self.repo, check=True,
                       capture_output=True)
        p = self.spawn(run, "w1", "--worktree", check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("left from an earlier, unfinished spawn", p.stderr)
        self.assertTrue(wt.is_dir())  # nothing was deleted

    def test_journal_and_log(self):
        run = self.init_run()
        self.spawn(run, "w1")
        self.spawn(run, "w2", env={"FAKE_HC_RUN_FAIL": "1"}, check=False)
        events = [json.loads(line) for line in self.hco("log", "--run", run, "--json").stdout.splitlines()]
        self.assertEqual([e["event"] for e in events], ["init", "spawn", "spawn_failed"])
        self.assertEqual(events[1]["worker"], "w1")
        self.assertIn("spawn_failed", self.hco("log", "--run", run, "--lines", "1").stdout)

    def test_runs_lists_runs_newest_first_without_hc(self):
        a = self.init_run()
        self.spawn(a, "w1")
        out = self.hco("runs", env={"HCO_HC": "/nonexistent/hc"}).stdout
        self.assertIn(f"{a}  ", out)
        self.assertIn("1 worker(s)", out)
        rows = json.loads(self.hco("runs", "--json").stdout)
        self.assertEqual(rows[0]["workers"], ["w1"])

    def test_error_exit_codes_and_messages_are_unchanged(self):
        p = self.hco("status", "--run", "nope", check=False)
        self.assertEqual(p.returncode, 1)
        self.assertTrue(p.stderr.startswith("hco: run 'nope' not found"))
        self.assertNotIn("Traceback", p.stderr)


class Check(FlowBase):
    def test_check_goes_into_the_brief_and_run_json(self):
        run = self.init_run()
        out, _, outbox = self.writer(run, "w1", "--check", "test -f a.txt")
        self.assertEqual(out["check"], "test -f a.txt")
        brief = (outbox / "BRIEF.md").read_text()
        self.assertIn("      test -f a.txt\n", brief)
        self.assertIn("Never weaken, skip or delete a test", brief)
        self.assertEqual(self.run_json(run)["workers"]["w1"]["check"], "test -f a.txt")

    def test_multiline_check_is_refused(self):
        run = self.init_run()
        p = self.spawn(run, "w1", "--check", "a\nb", check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("one line", p.stderr)

    def test_passing_check_is_recorded_with_log_and_terminal_removed(self):
        run = self.init_run()
        _, wt, _ = self.writer(run, "w1", "--check", "echo checking && test -f a.txt")
        (wt / "a.txt").write_text("a\n")
        p = self.hco("check", "--run", run, "--name", "w1")
        self.assertIn("check w1: PASSED", p.stdout)
        r = self.run_json(run)["workers"]["w1"]
        self.assertTrue(r["check_result"]["passed"])
        self.assertNotIn("check_run", r)
        self.assertEqual(Path(r["check_result"]["log"]).read_text(), "checking\n")
        names = [t["name"] for t in self.fake_state()["terminals"].values()]
        self.assertEqual(names, [f"{run}-w1"])  # the check terminal is gone
        self.assertEqual(self.fake_state()["terminals"]["t11"]["labels"]["hco.worker"], "w1")

    def test_failing_check_exits_1_and_shows_the_log_tail(self):
        run = self.init_run()
        self.writer(run, "w1", "--check", "echo boom >&2; exit 3")
        p = self.hco("check", "--run", run, "--name", "w1", check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("FAILED (exit 3)", p.stdout)
        self.assertIn("  | boom", p.stdout)
        self.assertFalse(self.run_json(run)["workers"]["w1"]["check_result"]["passed"])

    def test_slow_check_exits_124_and_a_second_call_picks_it_up(self):
        run = self.init_run()
        _, wt, _ = self.writer(run, "w1", "--check", "test -f a.txt")
        (wt / "a.txt").write_text("a\n")
        p = self.hco("check", "--run", run, "--name", "w1", env={"FAKE_HC_CHECK_SLOW": "1"}, check=False)
        self.assertEqual(p.returncode, 124)
        self.assertIn("still running", p.stdout)
        self.assertEqual(len(self.fake_state()["terminals"]), 2)
        self.assertEqual(self.row(run)["next"], "wait")  # no STATUS yet
        p = self.hco("check", "--run", run, "--name", "w1")
        self.assertIn("PASSED", p.stdout)
        self.assertEqual(len(self.fake_state()["terminals"]), 1)  # no second check terminal was started

    def test_cmd_sets_the_check_for_a_worker_that_had_none(self):
        run = self.init_run()
        self.writer(run, "w1")
        p = self.hco("check", "--run", run, "--name", "w1", check=False)
        self.assertIn("has no check", p.stderr)
        self.hco("check", "--run", run, "--name", "w1", "--cmd", "true")
        self.assertEqual(self.run_json(run)["workers"]["w1"]["check"], "true")

    def test_check_does_not_count_when_files_changed_while_it_ran(self):
        run = self.init_run()
        _, wt, _ = self.writer(run, "w1", "--check", "echo late > late.txt")
        p = self.hco("check", "--run", run, "--name", "w1", check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("NOT COUNTED", p.stdout)
        self.assertIn("files changed while the check ran: late.txt\n", p.stdout)
        self.assertEqual(self.row(run)["next"], "wait")

    def test_remote_worker_check_is_refused_with_the_way_out(self):
        run = self.init_run()
        self.set_up_far = Path(self.tmp.name) / "far"
        self.set_up_far.mkdir()
        out = self.hco("spawn", "--run", run, "--name", "w1", "--agent", "claude", "--on", "box", "--cwd",
                       str(self.set_up_far), "--brief", self.brief(), "--check", "true")
        self.assertIn("can't run it on another machine", "\n".join(json.loads(out.stdout)["warnings"]))
        p = self.hco("check", "--run", run, "--name", "w1", check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("only runs local checks", p.stderr)

    def test_merge_refuses_until_the_check_passed_on_the_current_files(self):
        run = self.init_run()
        _, wt, _ = self.writer(run, "w1", "--check", "test -f a.txt")
        (wt / "a.txt").write_text("a\n")
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo, capture_output=True, text=True).stdout
        p = self.hco("merge", "--run", run, check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("NOT VERIFIED w1: its check has not run", p.stdout)
        self.assertIn("!! its check has not run", self.hco("merge", "--run", run, "--dry-run").stdout)
        self.hco("check", "--run", run, "--name", "w1")
        (wt / "b.txt").write_text("sneaked in after the check\n")
        p = self.hco("merge", "--run", run, check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("files changed after its check passed", p.stdout)
        self.assertEqual(subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo, capture_output=True,
                                        text=True).stdout, head)  # nothing committed or merged
        self.assertIn("!! check `test -f a.txt`: passed, but files changed since", self.hco("collect", "--run", run).stdout)
        self.hco("check", "--run", run, "--name", "w1")
        self.assertIn("merged w1", self.hco("merge", "--run", run).stdout)
        w = self.run_json(run)["workers"]["w1"]
        self.assertEqual(w["merged"]["commit"], subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo,
                                                               capture_output=True, text=True).stdout.strip())
        # A rerun after the merge committed the worktree still sees the same content: no refusal.
        self.assertIn("skip w1", self.hco("merge", "--run", run).stdout)

    def test_merge_skip_checks_merges_and_says_so(self):
        run = self.init_run()
        _, wt, _ = self.writer(run, "w1", "--check", "false")
        (wt / "a.txt").write_text("a\n")
        self.hco("check", "--run", run, "--name", "w1", check=False)
        out = self.hco("merge", "--run", run, "--skip-checks").stdout
        self.assertIn("merging w1 although not verified as it is now (--skip-checks): its check failed (exit 1)", out)
        self.assertIn("merged w1", out)

    def test_a_worker_without_a_check_merges_as_before(self):
        run = self.init_run()
        _, wt, _ = self.writer(run, "w1")
        (wt / "a.txt").write_text("a\n")
        self.assertIn("merged w1", self.hco("merge", "--run", run).stdout)


class Accept(FlowBase):
    def test_accept_needs_done_and_a_clean_scope(self):
        run = self.init_run()
        _, wt, outbox = self.writer(run, "w1", "--scope", "src/**")
        p = self.hco("accept", "--run", run, "--name", "w1", check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("NOT ACCEPTED w1: STATUS is '(none)', not DONE", p.stdout)
        self.report(outbox)
        (wt / "stray.txt").write_text("x\n")
        p = self.hco("accept", "--run", run, "--name", "w1", check=False)
        self.assertIn("files outside its scope: stray.txt", p.stdout)
        (wt / "stray.txt").unlink()
        (wt / "src").mkdir()
        (wt / "src" / "a.py").write_text("a = 1\n")
        out = self.hco("accept", "--run", run, "--name", "w1", "--note", "read the diff").stdout
        self.assertIn("accepted w1. Next: hco merge", out)
        self.assertEqual(self.run_json(run)["workers"]["w1"]["accepted"]["note"], "read the diff")
        self.assertEqual(self.row(run)["next"], "merge")
        self.assertIn("accepted by the lead", self.hco("collect", "--run", run).stdout)

    def test_accept_needs_a_passed_check(self):
        run = self.init_run()
        _, wt, outbox = self.writer(run, "w1", "--check", "test -f a.txt")
        self.report(outbox)
        p = self.hco("accept", "--run", run, "--name", "w1", check=False)
        self.assertIn("its check has not run", p.stdout)
        (wt / "a.txt").write_text("a\n")
        self.hco("check", "--run", run, "--name", "w1")
        self.hco("accept", "--run", run, "--name", "w1")

    def test_force_records_what_was_overridden(self):
        run = self.init_run()
        self.writer(run, "w1")
        out = self.hco("accept", "--run", run, "--name", "w1", "--force").stdout
        self.assertIn("accepted w1 despite: STATUS is '(none)', not DONE", out)
        self.assertEqual(len(self.run_json(run)["workers"]["w1"]["accepted"]["forced"]), 1)

    def test_reviewer_that_changed_files_is_not_accepted(self):
        run = self.init_run()
        out = json.loads(self.hco("spawn", "--run", run, "--name", "rev", "--agent", "claude", "--role", "review",
                                  "--worktree", "--brief", self.brief()).stdout)
        self.report(Path(out["brief"]).parent)
        (Path(out["cwd"]) / "oops.txt").write_text("x")
        p = self.hco("accept", "--run", run, "--name", "rev", check=False)
        self.assertIn("review worker but files changed: oops.txt", p.stdout)

    def test_change_after_accept_blocks_the_merge(self):
        run = self.init_run()
        _, wt, outbox = self.writer(run, "w1")
        (wt / "a.txt").write_text("a\n")
        self.report(outbox)
        self.hco("accept", "--run", run, "--name", "w1")
        (wt / "a.txt").write_text("changed after the lead looked\n")
        self.assertEqual(self.row(run)["next"], "changed after accept: verify again")
        p = self.hco("merge", "--run", run, check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("files changed after you accepted it", p.stdout)
        self.hco("accept", "--run", run, "--name", "w1")
        self.assertIn("merged w1", self.hco("merge", "--run", run).stdout)
        self.assertEqual(self.row(run)["next"], "merged")


class FollowUp(FlowBase):
    def test_send_starts_a_new_round_and_keeps_the_old_report(self):
        run = self.init_run()
        _, _, outbox = self.writer(run, "w1")
        self.report(outbox, result="# round 1\n")
        self.hco("accept", "--run", run, "--name", "w1")
        out = self.hco("send", "--run", run, "--name", "w1", "--text", "Test x fails: fix it.").stdout
        self.assertIn("sent round 2 of at most 3 to w1", out)
        self.assertFalse((outbox / "STATUS").exists())  # the table can't show a stale DONE
        self.assertEqual((outbox / "STATUS.r1").read_text(), "DONE\n")
        self.assertEqual((outbox / "RESULT.r1.md").read_text(), "# round 1\n")
        self.assertIn("Test x fails: fix it.", (outbox / "FOLLOWUP.r2.md").read_text())
        target, text = self.fake_state()["sent"][0]
        self.assertEqual(target, "t11")
        self.assertIn(str(outbox / "FOLLOWUP.r2.md"), text)
        self.assertNotIn("\n", text)
        w = self.run_json(run)["workers"]["w1"]
        self.assertEqual(w["round"], 2)
        self.assertNotIn("accepted", w)  # the earlier verdict no longer covers the work
        self.assertEqual(self.row(run)["next"], "wait")

    def test_round_limit_exits_4_and_force_overrides(self):
        run = self.init_run()
        _, _, outbox = self.writer(run, "w1", "--max-rounds", "2")
        self.report(outbox)
        out = self.hco("send", "--run", run, "--name", "w1", "--text", "again").stdout
        self.assertIn("that was its last round", out)
        self.report(outbox)
        p = self.hco("send", "--run", run, "--name", "w1", "--text", "and again", check=False)
        self.assertEqual(p.returncode, 4)
        self.assertIn("report what is open to the user", p.stderr)
        self.assertEqual((outbox / "STATUS").read_text(), "DONE\n")  # untouched
        self.hco("send", "--run", run, "--name", "w1", "--text", "and again", "--force")
        self.assertEqual(self.run_json(run)["workers"]["w1"]["round"], 3)

    def test_send_without_a_report_is_refused(self):
        run = self.init_run()
        self.writer(run, "w1")
        p = self.hco("send", "--run", run, "--name", "w1", "--text", "hurry", check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("has not reported this round yet", p.stderr)

    def test_failed_send_puts_the_report_back(self):
        run = self.init_run()
        _, _, outbox = self.writer(run, "w1")
        self.report(outbox)
        p = self.hco("send", "--run", run, "--name", "w1", "--text", "x", env={"FAKE_HC_SEND_FAIL": "1"}, check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("follow-up NOT sent to w1", p.stderr)
        self.assertEqual((outbox / "STATUS").read_text(), "DONE\n")
        self.assertFalse((outbox / "STATUS.r1").exists())
        self.assertEqual(self.run_json(run)["workers"]["w1"]["round"], 1)

    def test_follow_up_file_and_brief_mention(self):
        run = self.init_run()
        _, _, outbox = self.writer(run, "w1")
        self.assertIn("FOLLOWUP.r<n>.md", (outbox / "BRIEF.md").read_text())
        self.report(outbox)
        f = Path(self.tmp.name) / "findings.md"
        f.write_text("1. src/a.py:3 off by one\n")
        self.hco("send", "--run", run, "--name", "w1", "--file", str(f))
        self.assertIn("1. src/a.py:3 off by one", (outbox / "FOLLOWUP.r2.md").read_text())

    def test_remote_worker_round_is_rotated_on_its_machine(self):
        run = self.init_run()
        far = Path(self.tmp.name) / "far"
        far.mkdir()
        out = json.loads(self.hco("spawn", "--run", run, "--name", "w1", "--agent", "claude", "--on", "box",
                                  "--cwd", str(far), "--brief", self.brief()).stdout)
        outbox = Path(out["brief"]).parent
        self.report(outbox)
        self.hco("send", "--run", run, "--name", "w1", "--text", "more")
        self.assertTrue((outbox / "STATUS.r1").is_file())
        self.assertTrue((outbox / "FOLLOWUP.r2.md").is_file())
        self.hco("cleanup", "--run", run)  # every round's files are archived before the remote outbox goes
        arch = self.repo / ".hc-orchestrate" / run / "workers" / "w1" / "out"
        self.assertEqual(sorted(p.name for p in arch.iterdir()), ["FOLLOWUP.r2.md", "RESULT.r1.md", "STATUS.r1"])


class NextStep(FlowBase):
    def test_next_column_follows_the_worker(self):
        run = self.init_run()
        _, wt, outbox = self.writer(run, "w1", "--check", "test -f a.txt")
        self.assertEqual(self.row(run)["next"], "wait")
        self.set_terminal(f"{run}-w1", status={"state": "blocked", "kind": "permission"})
        self.assertEqual(self.row(run)["next"], "person must answer (hc attach)")
        self.set_terminal(f"{run}-w1", status={"state": "idle"})
        self.assertEqual(self.row(run)["next"], "stopped without STATUS: nudge")
        self.set_terminal(f"{run}-w1", status=None, reported=False, turn=0)
        self.assertEqual(self.row(run)["next"], "no report yet: trust dialog?")
        self.report(outbox, "BLOCKED: needs tests/x.py (outside my scope)")
        self.assertEqual(self.row(run)["next"], "unblock (read RESULT.md)")
        self.report(outbox)
        self.assertEqual(self.row(run)["next"], "run check")
        self.hco("check", "--run", run, "--name", "w1", check=False)
        self.assertEqual(self.row(run)["next"], "check failed: send follow-up")
        (wt / "a.txt").write_text("a\n")
        self.hco("check", "--run", run, "--name", "w1")
        self.assertEqual(self.row(run)["next"], "verify, then accept")
        (outbox / "STATUS").unlink()
        self.set_terminal(f"{run}-w1", state="finished", status=None, exit_code=1)
        self.assertEqual(self.row(run)["next"], "exited without STATUS: read, respawn?")

    def test_deadline_flags_a_long_round_in_table_and_json(self):
        run = self.init_run()
        self.spawn(run, "w1", "--deadline", "30")
        self.assertEqual(self.row(run)["next"], "wait")
        self.set_worker(run, "w1", spawned=time.strftime(hco.TIME_FMT, time.localtime(time.time() - 45 * 60)))
        row = self.row(run)
        self.assertEqual(row["next"], "over its 30 min budget: hc read")
        self.assertIn(row["age_min"], (44, 45, 46))
        self.assertIn("note: w1 has been on round 1 for", self.hco("status", "--run", run).stdout)

    def test_status_after_cleanup_reads_the_archived_outbox(self):
        run = self.init_run()
        _, wt, outbox = self.writer(run, "w1")
        (wt / "a.txt").write_text("a\n")
        self.report(outbox)
        self.hco("merge", "--run", run)
        self.hco("cleanup", "--run", run, "--worktrees")
        self.assertFalse(wt.exists())
        self.assertEqual(self.row(run)["outbox"], "DONE")
        report = self.hco("report", "--run", run).stdout
        self.assertRegex(report, r"\| w1 \| claude \| implement \| 1 \| DONE \| - \| - \| [0-9a-f]{10} \|")
        self.assertIn("**merge** `w1`", report)
        self.assertTrue((self.repo / ".hc-orchestrate" / run / "REPORT.md").is_file())


class Restart(FlowBase):
    """The lead was restarted in a new terminal (t99); its workers were started by t1 with --outlive."""

    NEW = {"HC_TERMINAL_ID": "t99"}

    def test_wait_polls_when_the_workers_are_not_this_terminals_children(self):
        run = self.init_run()
        out = json.loads(self.spawn(run, "w1", "--outlive").stdout)
        args = ("wait", "--run", run, "--timeout", "1", "--interval", "0.1")
        first = self.hco(*args, env=self.NEW).stdout
        self.assertIn("wait: reason=timeout (polling", first)
        self.assertIn("run hco adopt", first)
        (Path(out["brief"]).parent / "STATUS").write_text("DONE\n")  # reported while nobody was waiting
        second = self.hco(*args, env=self.NEW).stdout
        self.assertIn("wait: reason=change", second)
        self.assertIn("changed: w1", second)
        self.assertIn("wait: reason=timeout", self.hco(*args, env=self.NEW).stdout)  # seen: not reported twice

    def test_wait_sees_a_change_while_it_polls(self):
        run = self.init_run()
        out = json.loads(self.spawn(run, "w1").stdout)
        args = ["wait", "--run", run, "--timeout", "20", "--interval", "0.1"]
        self.hco(*args[:4], "0", env=self.NEW)  # baseline
        p = subprocess.Popen([sys.executable, "-I", str(HCO), *args], cwd=self.repo, env={**self.env, **self.NEW},
                             stdout=subprocess.PIPE, text=True)
        time.sleep(0.5)
        (Path(out["brief"]).parent / "STATUS").write_text("FAILED: no\n")
        text, _ = p.communicate(timeout=15)
        self.assertIn("changed: w1", text)

    def test_wait_falls_back_to_polling_when_hc_reports_no_children(self):
        run = self.init_run()
        self.spawn(run, "w1")
        out = self.hco("wait", "--run", run, "--timeout", "0", env={"FAKE_HC_NO_CHILDREN": "1"}).stdout
        self.assertIn("(polling", out)

    def test_wait_with_children_is_unchanged(self):
        run = self.init_run()
        self.spawn(run, "w1")
        out = self.hco("wait", "--run", run, "--timeout", "1").stdout
        self.assertIn("wait: reason=timeout\n", out)
        self.assertNotIn("polling", out)

    def test_wait_without_any_terminal_still_points_to_preflight(self):
        run = self.init_run()
        p = self.hco("wait", "--run", run, "--timeout", "1", env={"FAKE_HC_NO_CHILDREN": "1"}, check=False)
        self.assertEqual(p.returncode, 2)

    def test_adopt_moves_the_run_to_the_new_lead(self):
        run = self.init_run()
        out = json.loads(self.spawn(run, "w1", "--outlive").stdout)
        gone = json.loads(self.spawn(run, "w2").stdout)
        self.set_terminal(gone["terminal"], state="finished", status=None)
        text = self.hco("adopt", "--run", run, env=self.NEW).stdout
        self.assertIn("lead is now t99 (was t1)", text)
        self.assertIn("w1: brief updated, told the worker", text)
        self.assertIn("w2: terminal is gone", text)
        data = self.run_json(run)
        self.assertEqual(data["lead"], "t99")
        self.assertEqual(data["former_leads"][0]["lead"], "t1")
        brief = Path(out["brief"]).read_text()
        self.assertIn('hc send t99 "[hco', brief)
        self.assertIn("hc terminal `t99`", brief)
        self.assertNotIn("hc send t1 ", brief)
        sent = self.fake_state()["sent"]
        self.assertEqual(len(sent), 1)
        self.assertIn("hc send t99", sent[0][1])
        self.assertIn("already belongs", self.hco("adopt", "--run", run, env=self.NEW).stdout)


class Plan(FlowBase):
    def plan(self, run, workers, *extra, check=True, **top):
        d = Path(self.tmp.name) / "plan"
        d.mkdir(exist_ok=True)
        if not (d / "task.md").exists():
            (d / "task.md").write_text("## Goal\nDo it.\n")
        for w in workers:
            w.setdefault("brief", "task.md")
            w.setdefault("agent", "claude")
        (d / "plan.json").write_text(json.dumps({"workers": workers, **top}))
        return self.hco("plan", "--run", run, "--file", str(d / "plan.json"), *extra, check=check)

    def advance(self, run, *extra, **kw):
        return json.loads(self.hco("advance", "--run", run, *extra, **kw).stdout)

    def test_plan_is_validated_as_a_whole_and_nothing_is_stored_on_error(self):
        run = self.init_run()
        p = self.plan(run, [
            {"name": "a", "after": ["b"], "colour": "red"},
            {"name": "b", "after": ["a", "ghost", "a:finished"]},
            {"name": "rev", "role": "review", "scope": ["src/**"]},
            {"name": "x", "brief": "missing.md"},
            {"name": "m", "after": ["rev:merged"]},
            {"name": "c", "cwd": "@a"},
            {"name": "Bad Name"},
        ], check=False)
        self.assertEqual(p.returncode, 1)
        for want in ("a: unknown key(s) colour", "dependency cycle: a -> b -> a", "after 'ghost' names no worker",
                     "after 'a:finished': the part after ':' is one of done, accepted, merged",
                     "rev: a review worker may change no files", "x: brief file 'missing.md' is missing or empty",
                     "after 'rev:merged': only a worktree worker can be merged",
                     "c: cwd '@a' (that worker's folder) needs 'a' in its \"after\" list", "Bad Name: names are"):
            self.assertIn(want, p.stderr)
        self.assertNotIn("plan", self.run_json(run))

    def test_overlapping_scopes_of_parallel_writers_are_refused(self):
        run = self.init_run()
        two = [{"name": "a", "worktree": True, "scope": ["src/**"], "check": "true"},
               {"name": "b", "worktree": True, "scope": ["src/api/x.py"], "check": "true"}]
        p = self.plan(run, [dict(w) for w in two], check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("a and b can run at the same time and their scopes may share files (src/** / src/api/x.py)",
                      p.stderr)
        out = self.plan(run, [dict(w) for w in two], "--allow-overlap").stdout
        self.assertIn("warning: a and b can run at the same time", out)
        two[1]["after"] = ["a:merged"]  # one after the other: no clash
        self.assertNotIn("share files", self.plan(run, two).stdout)

    def test_plan_warns_about_unguarded_writers_and_shared_checkouts(self):
        run = self.init_run()
        out = self.plan(run, [{"name": "a"}, {"name": "b"}]).stdout
        self.assertIn("warning: a and b may write in the same checkout at the same time", out)
        self.assertIn("warning: a: writer without a scope", out)
        self.assertIn("warning: a: writer without a check", out)
        self.assertIn("step 1: a (claude, implement), b (claude, implement)", out)

    def test_build_review_merge_docs_pipeline(self):
        run = self.init_run()
        out = self.plan(run, [
            {"name": "build", "worktree": True, "scope": ["src/**"], "check": "test -f src/a.py", "deadline_min": 20},
            {"name": "rev", "agent": "codex", "role": "review", "cwd": "@build", "after": ["build:done"]},
            {"name": "docs", "worktree": True, "scope": ["docs/**"], "check": "true", "after": ["build:merged"]},
        ]).stdout
        self.assertIn("step 2: rev (codex, review, after build:done), docs (claude, implement, after build:merged)", out)
        self.assertEqual(self.advance(run, "--dry-run")["started"], [{"worker": "build", "dry_run": True}])
        self.assertEqual(self.run_json(run)["workers"], {})

        res = self.advance(run)
        self.assertEqual([s["worker"] for s in res["started"]], ["build"])
        self.assertEqual(res["waiting"], {"rev": ["build is still working"], "docs": [
            f"build is not merged yet (verify it, then hco merge --run {run} --name build)"]})
        self.assertFalse(res["complete"])
        build = self.run_json(run)["workers"]["build"]
        self.assertEqual((build["scope"], build["check"], build["deadline_min"]), (["src/**"], "test -f src/a.py", 20))
        self.assertEqual(self.advance(run)["started"], [])  # nothing new: advancing twice starts nothing twice

        wt, outbox = Path(build["cwd"]), Path(build["outbox"])
        (wt / "src").mkdir()
        (wt / "src" / "a.py").write_text("a = 1\n")
        self.report(outbox)
        self.assertEqual(self.advance(run)["waiting"]["rev"],
                         [f"build is DONE but its check has not passed (hco check --run {run} --name build)"])
        self.hco("check", "--run", run, "--name", "build")
        res = self.advance(run)
        self.assertEqual([s["worker"] for s in res["started"]], ["rev"])
        rev = self.run_json(run)["workers"]["rev"]
        self.assertEqual(rev["cwd"], build["cwd"])  # the reviewer works in the builder's worktree
        self.assertEqual((rev["agent"], rev["role"], rev["after"]), ("codex", "review", ["build:done"]))
        brief = (Path(rev["outbox"]) / "BRIEF.md").read_text()
        self.assertIn("### Inputs from earlier workers", brief)
        self.assertIn(f"`build` (claude, implement): its report is `{build['outbox']}/RESULT.md`", brief)
        self.assertIn("(not merged yet)", brief)

        self.report(Path(rev["outbox"]))
        self.hco("accept", "--run", run, "--name", "rev")
        self.assertIn("then: hco advance", self.hco("accept", "--run", run, "--name", "build").stdout)
        self.hco("merge", "--run", run, "--name", "build")
        res = self.advance(run)
        self.assertEqual([s["worker"] for s in res["started"]], ["docs"])
        self.assertEqual(res["not_yet_accepted"], ["docs"])
        docs = self.run_json(run)["workers"]["docs"]
        self.assertTrue((Path(docs["cwd"]) / "src" / "a.py").is_file())  # its worktree starts from the merge
        self.report(Path(docs["outbox"]))
        self.hco("check", "--run", run, "--name", "docs")
        self.hco("accept", "--run", run, "--name", "docs")
        self.assertTrue(self.advance(run)["complete"])

    def test_default_dependency_waits_for_the_leads_accept(self):
        run = self.init_run()
        self.plan(run, [{"name": "research", "role": "research"},
                        {"name": "impl", "worktree": True, "scope": ["src/**"], "check": "true", "after": ["research"]}])
        self.advance(run)
        research = self.run_json(run)["workers"]["research"]
        self.report(Path(research["outbox"]))
        self.assertEqual(self.advance(run)["waiting"]["impl"], [
            f"research is DONE and waits for you: verify it, then hco accept --run {run} --name research"])
        self.hco("accept", "--run", run, "--name", "research")
        self.assertEqual([s["worker"] for s in self.advance(run)["started"]], ["impl"])

    def test_failed_dependency_is_named(self):
        run = self.init_run()
        self.plan(run, [{"name": "a", "role": "research"}, {"name": "b", "role": "research", "after": ["a"]}])
        self.advance(run)
        self.report(Path(self.run_json(run)["workers"]["a"]["outbox"]), "FAILED: no access")
        self.assertEqual(self.advance(run)["waiting"]["b"], ["a reported FAILED: no access"])

    def test_max_parallel_limits_working_workers(self):
        run = self.init_run()
        ws = [{"name": n, "worktree": True, "scope": [f"{n}/**"], "check": "true"} for n in ("a", "b", "c")]
        self.plan(run, ws, max_parallel=2)
        res = self.advance(run)
        self.assertEqual([s["worker"] for s in res["started"]], ["a", "b"])
        self.assertEqual(res["waiting"]["c"], ["ready, but 2 worker(s) are working (max_parallel 2)"])
        self.assertEqual(self.advance(run)["started"], [])
        self.report(Path(self.run_json(run)["workers"]["a"]["outbox"]))  # a reported: its slot is free
        self.assertEqual([s["worker"] for s in self.advance(run)["started"]], ["c"])

    def test_a_spawn_error_does_not_stop_the_others(self):
        run = self.init_run()
        self.plan(run, [{"name": "a", "role": "research", "cwd": "/nonexistent/dir"}, {"name": "b", "role": "research"}])
        p = self.hco("advance", "--run", run, check=False)
        res = json.loads(p.stdout)
        self.assertEqual(p.returncode, 1)
        self.assertIn("is not a folder", res["errors"]["a"])
        self.assertEqual([s["worker"] for s in res["started"]], ["b"])
        self.assertEqual(res["not_started"], ["a"])

    def test_replanning_keeps_started_workers_and_the_brief_copy(self):
        run = self.init_run()
        self.plan(run, [{"name": "a", "role": "research"}])
        self.advance(run)
        (Path(self.tmp.name) / "plan" / "task.md").write_text("## Goal\nChanged later.\n")
        out = self.plan(run, [{"name": "a", "role": "review"}, {"name": "b", "role": "research", "after": ["a"]}]).stdout
        self.assertIn("note: a is already running; its entry was left as it is", out)
        plan = json.loads(self.hco("plan", "--run", run).stdout)
        self.assertEqual([(e["name"], e["role"]) for e in plan["workers"]], [("a", "research"), ("b", "research")])
        stored = self.repo / ".hc-orchestrate" / run / "plan"
        self.assertIn("Do it.", (stored / "a.md").read_text())
        self.assertIn("Changed later.", (stored / "b.md").read_text())

    def test_advance_without_a_plan(self):
        run = self.init_run()
        p = self.hco("advance", "--run", run, check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("has no plan", p.stderr)


class Units(unittest.TestCase):
    def test_scopes_may_overlap(self):
        for p, q, want in [
            ("src/api/**", "src/ui/**", False),
            ("src/**", "src/api/x.py", True),
            ("src", "src/api/x.py", True),          # a folder covers what is under it
            ("*.md", "docs/**", False),              # *.md is root-level only
            ("**/*.md", "docs/**", True),
            ("src/*.py", "src/a.py", True),
            ("src/*.py", "src/a.ts", False),
            ("src/a*", "src/b*", True),              # two wildcards: assumed to meet (errs on flagging)
            ("docs/", "docs/guide.md", True),
            ("a/b/c.txt", "a/b/c.txt", True),
            ("a/b/c.txt", "a/b/d.txt", False),
            ("tests/test_[ab].py", "tests/test_c.py", False),
        ]:
            self.assertEqual(hco.scopes_may_overlap(p, q), want, f"{p!r} vs {q!r}")
            self.assertEqual(hco.scopes_may_overlap(q, p), want, f"{q!r} vs {p!r}")

    def test_tree_hash_survives_a_commit_and_ignores_outboxes_and_the_index(self):
        with tempfile.TemporaryDirectory() as d:
            def g(*args):
                return subprocess.run(["git", *args], cwd=d, check=True, capture_output=True, text=True).stdout
            g("init", "-q")
            g("config", "user.email", "t@t")
            g("config", "user.name", "t")
            self.assertIsNotNone(hco.tree_hash(d))  # no commit yet
            Path(d, "a.txt").write_text("a\n")
            g("add", "-A")
            g("commit", "-q", "-m", "base")
            base = hco.tree_hash(d)
            self.assertEqual(base, g("rev-parse", "HEAD^{tree}").strip())
            Path(d, "new.txt").write_text("n\n")
            Path(d, "a.txt").write_text("changed\n")
            dirty = hco.tree_hash(d)
            self.assertNotEqual(dirty, base)
            self.assertEqual(g("status", "--porcelain"), " M a.txt\n?? new.txt\n")  # the real index is untouched
            for box in (".hc-orchestrate/r/out/w/STATUS", "pkg/.hc-orchestrate/r/out/w/STATUS"):
                Path(d, box).parent.mkdir(parents=True)
                Path(d, box).write_text("DONE\n")
            self.assertEqual(hco.tree_hash(d), dirty)  # outboxes don't count, excluded from git or not
            g("add", "-A", "--", "a.txt", "new.txt")
            g("commit", "-q", "-m", "work")
            self.assertEqual(hco.tree_hash(d), dirty)  # same content, now committed
            Path(d, "a.txt").write_text("again\n")
            self.assertNotEqual(hco.tree_hash(d), dirty)
        self.assertIsNone(hco.tree_hash(d))  # folder gone

    def test_minutes_since(self):
        self.assertEqual(hco.minutes_since(time.strftime(hco.TIME_FMT)), 0)
        self.assertIsNone(hco.minutes_since("not a time"))
        self.assertIsNone(hco.minutes_since(None))


if __name__ == "__main__":
    unittest.main()
