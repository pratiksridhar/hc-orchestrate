"""Tests for team runs (`hco init --team`): scripts/hcteam.py and its side in scripts/hco.py. The doc
store is tests/fake_core.py, hc is tests/fake_hc.py; no real hc is involved.

Run: python3 -I -m unittest discover -s tests -v
"""
import json
import os
import shlex
import subprocess
import sys
import threading
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from test_hco import HCO, Base  # noqa: E402

import hcdoc  # noqa: E402
import hcteam  # noqa: E402

TEAM = HCO.parent / "hcteam.py"
FAKE_CORE = HERE / "fake_core.py"
CORE_ENV = ("FAKE_CORE_DIR", "HCDOC_TRANSPORT", "HCDOC_CONNECT")


class TeamBase(Base):
    def setUp(self):
        super().setUp()
        self.core = Path(self.tmp.name) / "core"
        self.core.mkdir()
        self.env = {k: v for k, v in self.env.items() if not k.startswith("HCDOC_")}
        self.env.update(FAKE_CORE_DIR=str(self.core), HCDOC_TRANSPORT="connect",
                        HCDOC_CONNECT=f"{shlex.quote(sys.executable)} {shlex.quote(str(FAKE_CORE))} connect")

    def docs(self):
        f = self.core / "docs.json"
        return json.loads(f.read_text()) if f.exists() else {}

    def team(self, *names):
        """A team run with one worker per name. Sets self.run and self.addr ({name: terminal})."""
        self.run_id = json.loads(self.hco("init", "--task", "t", "--team").stdout)["run"]
        self.addr = {n: json.loads(self.spawn(self.run_id, n).stdout)["address"] for n in names}
        return self.run_id

    def w(self, name, verb, *args, check=True, term=None):
        """Run an hcteam command as worker `name`, from its terminal."""
        env = {**self.env, "HC_TERMINAL_ID": term or self.addr[name]}
        p = subprocess.run([sys.executable, "-I", str(TEAM), verb, "--run", self.run_id, "--as", name, *args],
                           cwd=self.repo, env=env, capture_output=True, text=True)
        if check and p.returncode != 0:
            self.fail(f"hcteam {verb} {args} as {name} exited {p.returncode}\n{p.stdout}\n{p.stderr}")
        return p

    def add(self, title, *extra, **kw):
        return self.hco("task", "add", "--run", self.run_id, "--title", title, *extra, **kw)

    def lead_terminal(self, addr):
        """Make the lead's terminal known to fake hc, so that `hc send` to it is recorded."""
        f = Path(self.env["FAKE_HC_STATE"])
        st = json.loads(f.read_text())
        st["terminals"][addr] = {"address": addr, "name": "lead", "labels": {}, "state": "live"}
        f.write_text(json.dumps(st))

    def run_json(self):
        return json.loads((self.repo / ".hc-orchestrate" / self.run_id / "run.json").read_text())

    def sent(self):
        """What fake hc was asked to `hc send`: [[address, text], ...]."""
        return json.loads(Path(self.env["FAKE_HC_STATE"]).read_text()).get("sent", [])

    def session(self):
        os.environ.update({k: self.env[k] for k in CORE_ENV})
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in CORE_ENV])
        s = hcdoc.Session()
        self.addCleanup(s.close)
        return s

    def journal(self, event):
        lines = self.hco("log", "--run", self.run_id, "--json").stdout.splitlines()
        return [e for e in map(json.loads, lines) if e["event"] == event]


class Setup(TeamBase):
    def test_init_opens_the_team_with_the_lead_as_member(self):
        out = json.loads(self.hco("init", "--task", "t", "--team").stdout)
        doc = self.docs()[out["team"]]["value"]
        self.assertEqual(out["team"], f"sk-{out['run']}-team")
        self.assertEqual(doc["members"], {"lead": {"term": "t1", "role": "lead", "cursor": 0}})

    def test_a_run_without_team_never_touches_the_doc_store(self):
        run = self.init_run()
        brief = Path(json.loads(self.spawn(run).stdout)["brief"]).read_text()
        self.assertNotIn("Your team", brief)
        self.assertEqual(self.docs(), {})
        for cmd in (("tasks",), ("mail",), ("task", "add", "--title", "x")):
            self.assertEqual(self.hco(*cmd, "--run", run, check=False).returncode, 2)
        out = self.hco("collect", "--run", run).stdout + self.hco("status", "--run", run).stdout
        self.assertNotIn("team", out)

    def test_init_team_without_a_doc_store_exits_4_and_creates_nothing(self):
        p = self.hco("init", "--task", "t", "--team", env={"HCDOC_CONNECT": "false"}, check=False)
        self.assertEqual(p.returncode, 4)
        self.assertIn("doc store", p.stderr)
        self.assertFalse((self.repo / ".hc-orchestrate").exists())

    def test_a_team_name_used_before_is_not_reused(self):
        s = self.session()
        self.assertTrue(hcteam.open_team(s, "abcd", "t1"))
        self.assertFalse(hcteam.open_team(s, "abcd", "t1"))
        hcteam.close_team(s, "abcd")
        self.assertFalse(hcteam.open_team(s, "abcd", "t1"))  # its old slots would read as deleted docs

    def test_spawn_makes_members_and_briefs_only_where_the_team_works(self):
        run = self.team("api")
        rec = self.run_json()["workers"]["api"]
        brief = Path(rec["outbox"], "BRIEF.md").read_text()
        self.assertIn(f"hcteam.py claim --run {run} --as api", brief)
        self.assertIn(f"hcteam.py send --run {run} --as api --to <name>", brief)
        self.assertLess(brief.index("## Your team"), brief.index("## How to report"))
        self.assertTrue(rec["team"])
        members = self.docs()[f"sk-{run}-team"]["value"]["members"]
        self.assertEqual(members["api"], {"cursor": 0, "term": self.addr["api"], "role": "implement", "agent": "claude"})
        safe = json.loads(self.spawn(run, "safe", "--permissions", "safe").stdout)
        self.assertNotIn("Your team", Path(safe["brief"]).read_text())
        self.assertTrue(any("not a team member" in x for x in safe["warnings"]))
        far_dir = Path(self.tmp.name) / "far"  # fake hc writes "remote" files locally: keep them in the temp dir
        far = json.loads(self.spawn(run, "far", "--on", "box", "--cwd", str(far_dir)).stdout)
        self.assertTrue(any("not a team member" in x for x in far["warnings"]))
        self.assertEqual(sorted(self.docs()[f"sk-{run}-team"]["value"]["members"]), ["api", "lead"])
        self.assertEqual(self.spawn(run, "lead", check=False).returncode, 1)  # reserved as a mail address
        self.assertIn("api", self.w("api", "members").stdout)


class Tasks(TeamBase):
    def test_claim_do_complete_in_order(self):
        self.team("api", "ui")
        self.assertIn("task #1 added", self.add("write the limiter", "--detail", "token bucket, per key").stdout)
        self.add("wire it into the router", "--after", "1")
        out = self.w("api", "claim").stdout
        self.assertIn("task #1 is yours: write the limiter", out)
        self.assertIn("token bucket, per key", out)
        p = self.w("ui", "claim", check=False)  # #1 is taken, #2 waits for it
        self.assertEqual(p.returncode, 3)
        self.assertIn("#1 (api is working on it); #2 (it waits for task(s) #1)", p.stderr)
        self.assertIn("report to your lead", p.stderr)
        self.assertEqual(self.w("api", "claim", check=False).returncode, 3)  # one task at a time
        self.assertIn("blocked (waits for #1)", self.w("ui", "tasks").stdout)
        self.assertIn("task #1 completed", self.w("api", "done", "1", "--note", "limiter.py, 6 tests pass").stdout)
        self.assertIn("task #2 is yours", self.w("ui", "claim").stdout)
        self.w("ui", "done", "2", "--note", "router.py calls it")
        p = self.w("api", "claim", check=False)
        self.assertIn("every task is completed", p.stderr)
        table = self.hco("tasks", "--run", self.run_id).stdout
        self.assertIn("0 pending, 0 in progress, 2 completed", table)
        self.assertIn("note: limiter.py, 6 tests pass", table)
        self.assertIn("a claim by the workers", table)

    def test_only_one_of_many_claimers_gets_a_task(self):
        names = ["a", "b", "c", "d"]
        self.team(*names)
        for i in range(3):
            self.add(f"task {i}")
        got = {}

        def work(n):
            got[n] = self.w(n, "claim", check=False)

        threads = [threading.Thread(target=work, args=(n,)) for n in names]
        [t.start() for t in threads]
        [t.join() for t in threads]
        won = sorted(n for n in names if got[n].returncode == 0)
        self.assertEqual(len(won), 3, {n: got[n].stderr for n in names})
        owners = [t["owner"] for t in json.loads(self.hco("tasks", "--run", self.run_id, "--json").stdout)]
        self.assertEqual(sorted(owners), won)  # three tasks, three different holders

    def test_assignment_and_release(self):
        self.team("api", "ui")
        self.add("ui only", "--for", "ui")
        self.add("anyone")
        self.assertIn("task #2 is yours", self.w("api", "claim").stdout)  # #1 is not api's to take
        self.assertEqual(self.w("api", "claim", "1", check=False).returncode, 3)
        self.assertIn("pending again", self.w("api", "release", "2", "--why", "needs src/ui, outside my scope").stdout)
        t2 = json.loads(self.hco("tasks", "--run", self.run_id, "--json").stdout)[1]
        self.assertEqual((t2["state"], t2["owner"], t2["note"]),
                         ("pending", None, "released by api: needs src/ui, outside my scope"))
        self.assertIn("task #1 is yours", self.w("ui", "claim").stdout)
        self.assertEqual(self.w("api", "done", "1", "--note", "not mine", check=False).returncode, 3)
        self.assertEqual(self.hco("task", "add", "--run", self.run_id, "--title", "x", "--for", "nobody",
                                  check=False).returncode, 2)
        self.assertEqual(self.add("x", "--after", "9", check=False).returncode, 2)

    def test_a_worker_acts_under_its_own_name_only(self):
        self.team("api", "ui")
        self.add("one")
        p = self.w("api", "claim", term=self.addr["ui"], check=False)
        self.assertEqual(p.returncode, 3)
        self.assertIn("use your own name", p.stderr)
        self.assertEqual(self.w("ghost", "claim", term="t99", check=False).returncode, 2)
        self.assertEqual(self.w("api", "send", "--to", "ui", "hi", term=self.addr["ui"], check=False).returncode, 3)

    def test_lead_reopens_assigns_and_cancels(self):
        run = self.team("api", "ui")
        self.add("one")
        self.add("two", "--after", "1")
        self.w("api", "claim")
        self.w("api", "done", "1", "--note", "done")
        self.assertEqual(self.hco("task", "reopen", "--run", run, "--id", "1", check=False).returncode, 2)  # no --why
        out = self.hco("task", "reopen", "--run", run, "--id", "1", "--why", "test_burst fails").stdout
        self.assertIn("it is pending, for api", out)
        self.assertEqual(self.w("ui", "claim", check=False).returncode, 3)  # back to api; #2 waits again
        self.hco("task", "assign", "--run", run, "--id", "1", "--for", "ui")
        self.assertIn("task #1 is yours", self.w("ui", "claim").stdout)
        self.assertEqual(self.hco("task", "assign", "--run", run, "--id", "1", check=False).returncode, 3)  # held
        self.hco("task", "cancel", "--run", run, "--id", "1", "--why", "not needed")
        self.assertEqual(self.w("ui", "done", "1", "--note", "x", check=False).returncode, 3)
        self.assertIn("task #2 is yours", self.w("api", "claim").stdout)  # a cancelled task blocks nothing
        self.assertNotIn("#1 ", self.w("api", "tasks").stdout)  # workers don't see cancelled tasks
        self.assertEqual([e["event"] for e in self.journal("task_reopen") + self.journal("task_cancel")],
                         ["task_reopen", "task_cancel"])

    def test_tasks_from_a_file_with_keys(self):
        run = self.team("api")
        f = Path(self.tmp.name) / "tasks.json"
        f.write_text(json.dumps([{"key": "model", "title": "the model", "for": "api"},
                                 {"title": "the view", "after": ["model"], "detail": "see docs/view.md"}]))
        out = self.hco("task", "add", "--run", run, "--file", str(f)).stdout
        self.assertIn("task #2 added: the view (after #1)", out)
        f.write_text(json.dumps([{"title": "x", "after": ["nope"]}]))
        self.assertEqual(self.hco("task", "add", "--run", run, "--file", str(f), check=False).returncode, 2)
        self.assertEqual(len(json.loads(self.hco("tasks", "--run", run, "--json").stdout)), 2)

    def test_idle_members_are_told_about_open_tasks(self):
        run = self.team("api", "ui")
        out = self.add("one", "--for", "ui").stdout
        self.assertIn("told ui that a task is open", out)
        self.assertEqual([a for a, _ in self.sent()], [self.addr["ui"]])
        self.assertIn(f"claim --run {run} --as ui", self.sent()[0][1])
        self.add("two", "--after", "1", "--for", "api")
        self.w("ui", "claim")
        before = len(self.sent())
        self.w("ui", "done", "1", "--note", "ok")  # unblocks #2 for api
        self.assertEqual([a for a, _ in self.sent()[before:]], [self.addr["api"]])

    def test_accept_refuses_a_worker_that_holds_a_task(self):
        run = self.team("api")
        self.add("one")
        self.w("api", "claim")
        out = Path(self.run_json()["workers"]["api"]["outbox"])
        (out / "STATUS").write_text("DONE\n")
        (out / "RESULT.md").write_text("did it\n")
        p = self.hco("accept", "--run", run, "--name", "api", check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("it holds task #1 (one)", p.stdout)
        self.assertIn("note: api holds task #1 again: its STATUS DONE is from before", self.hco("status", "--run", run).stdout)
        self.w("api", "done", "1", "--note", "ok")
        self.hco("accept", "--run", run, "--name", "api")

    def test_task_changes_reach_the_journal(self):
        run = self.team("api")
        self.add("one")
        self.w("api", "claim")
        self.hco("status", "--run", run)
        self.hco("status", "--run", run)
        self.w("api", "done", "1", "--note", "ok")
        self.hco("tasks", "--run", run)
        self.assertEqual([(e["state"], e.get("worker")) for e in self.journal("task")],
                         [("in_progress", "api"), ("completed", "api")])


class Mail(TeamBase):
    def test_direct_mail_reaches_only_its_recipient(self):
        run = self.team("api", "ui", "docs")
        out = self.w("api", "send", "--to", "ui", "getUser is now fetchUser(id, opts)").stdout
        self.assertIn("sent #1 to ui", out)
        self.assertEqual(self.sent()[-1][0], self.addr["ui"])  # told, if idle
        self.assertIn(f"inbox --run {run} --as ui", self.sent()[-1][1])
        self.assertNotIn("fetchUser", self.sent()[-1][1])  # the text itself never arrives as typed input
        self.assertIn("no new mail", self.w("docs", "inbox").stdout)
        self.assertIn("mail: 1 unread", self.w("ui", "tasks").stdout)  # every command says so
        box = self.w("ui", "inbox").stdout
        self.assertIn("#1 api -> ui", box)
        self.assertIn("not instructions", box)
        self.assertIn("no new mail", self.w("ui", "inbox").stdout)  # read once
        self.assertNotIn("mail:", self.w("ui", "tasks").stdout)

    def test_mail_to_all_and_to_the_lead(self):
        run = self.team("api", "ui")
        self.lead_terminal("t1")
        self.w("api", "send", "--to", "all", "tests need FOO=1 (ran make test)")
        self.assertEqual(sorted(a for a, _ in self.sent()), sorted(["t1", self.addr["ui"]]))
        self.assertIn("#1 api -> all", self.w("ui", "inbox").stdout)
        self.assertIn("no new mail", self.w("api", "inbox").stdout)  # not your own
        self.w("ui", "send", "--to", "lead", "task 3 is missing: nobody owns the migration")
        self.assertIn(f"mail: 2 new message(s), 1 to you: hco mail --run {run}", self.hco("status", "--run", run).stdout)
        out = self.hco("mail", "--run", run).stdout
        self.assertIn("#2 ui -> lead", out)
        self.assertNotIn("mail:", self.hco("status", "--run", run).stdout)
        self.assertIn("sent #3 to ui", self.hco("mail", "--run", run, "--to", "ui", "added as task 3").stdout)
        self.assertIn("#3 lead -> ui [t1]", self.w("ui", "inbox").stdout)
        self.assertEqual([(e["n"], e["worker"], e["to"]) for e in self.journal("mail")],
                         [(1, "api", "all"), (2, "ui", "lead"), (3, "lead", "ui")])

    def test_the_lead_sees_mail_between_workers_and_flags_a_wrong_author(self):
        run = self.team("api", "ui")
        self.w("api", "send", "--to", "ui", "between two workers")
        s = self.session()  # written past the tool's own check, as a worker with a patched script could
        hcteam.send(s, run, "api", self.addr["ui"], "ui", "ui posing as api", check_term=False)
        out = self.hco("collect", "--run", run).stdout
        self.assertIn("=== team mail: 2 message(s)", out)
        self.assertIn("#1 api -> ui", out)
        self.assertNotIn("!! message #1", out)
        self.assertIn(f"!! message #2 came from terminal {self.addr['ui']}, not api's ({self.addr['api']})", out)
        self.assertIn("=== team tasks", out)
        rows = json.loads(self.hco("mail", "--run", run, "--json", "--since", "1").stdout)
        self.assertEqual([r["n"] for r in rows], [2])
        self.assertTrue(rows[0]["flag"])

    def test_bad_mail_is_refused(self):
        self.team("api", "ui")
        for args in (("--to", "nobody", "hi"), ("--to", "api", "to myself"), ("--to", "ui", "x" * 501),
                     ("--to", "ui", " \n ")):
            self.assertEqual(self.w("api", "send", *args, check=False).returncode, 2, args)
        self.w("api", "send", "--to", "ui", "line one\nline two\x1b[31m red\x07  end")
        self.assertIn("line one line two [31m red end", self.w("ui", "inbox").stdout)

    def test_mail_limit_per_author(self):
        run = self.team("api", "ui")
        s = self.session()
        for i in range(hcteam.MAX_MAIL_PER_AUTHOR):
            hcteam.send(s, run, "api", self.addr["api"], "ui", f"m{i}")
        with self.assertRaises(hcteam.Refused):
            hcteam.send(s, run, "api", self.addr["api"], "ui", "one too many")
        hcteam.send(s, run, "ui", self.addr["ui"], "api", "others still can")


class Lifecycle(TeamBase):
    def test_an_unreachable_store_does_not_stop_the_run(self):
        run = self.team("api")
        down = {"HCDOC_CONNECT": "false"}
        self.assertIn("team: task list and mail not reachable", self.hco("status", "--run", run, env=down).stdout)
        self.assertIn("=== team: task list and mail not reachable", self.hco("collect", "--run", run, env=down).stdout)
        self.assertIn("could not close the team", self.hco("cleanup", "--run", run, env=down).stdout)
        self.assertNotIn("closed", self.run_json()["team"])
        self.assertIn("closed the team", self.hco("cleanup", "--run", run).stdout)  # a rerun finishes it

    def test_cleanup_archives_and_deletes(self):
        run = self.team("api", "ui")
        self.add("one")
        self.w("api", "claim")
        self.w("api", "done", "1", "--note", "npm test passes on node 20")
        self.w("api", "send", "--to", "ui", "node 18 fails: use 20")
        out = self.hco("cleanup", "--run", run).stdout
        self.assertIn("closed the team: 1 task(s) and 1 message(s)", out)
        self.assertTrue(all(d["value"] is None for d in self.docs().values()))
        arch = json.loads((self.repo / ".hc-orchestrate" / run / "team.json").read_text())
        self.assertEqual(arch["tasks"][0]["note"], "npm test passes on node 20")
        self.assertEqual(sorted(arch["team"]["members"]), ["api", "lead", "ui"])
        self.assertEqual(self.w("api", "claim", check=False).returncode, 3)
        self.assertIn("from the archive): 1 message(s)", self.hco("mail", "--run", run).stdout)
        self.assertIn("note: npm test passes on node 20", self.hco("tasks", "--run", run).stdout)
        self.assertIn("#1 api -> ui", self.hco("collect", "--run", run).stdout)
        self.assertEqual(self.add("late", check=False).returncode, 3)
        self.assertNotIn("the team", self.hco("cleanup", "--run", run).stdout)  # nothing left to close
        self.assertEqual(len(self.journal("mail")), 1)

    def test_adopt_moves_the_lead_member(self):
        run = self.team("api")
        self.lead_terminal("t5")
        self.hco("adopt", "--run", run, "--quiet", env={"HC_TERMINAL_ID": "t5"})
        self.assertEqual(self.docs()[f"sk-{run}-team"]["value"]["members"]["lead"]["term"], "t5")
        self.w("api", "send", "--to", "lead", "hello")
        self.assertEqual(self.sent()[-1][0], "t5")
        self.assertNotIn("!!", self.hco("mail", "--run", run, env={"HC_TERMINAL_ID": "t5"}).stdout)


if __name__ == "__main__":
    unittest.main()
