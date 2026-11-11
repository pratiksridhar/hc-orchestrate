"""Live tests for hco and team mode against a real herdr-core, with scripted workers.

The workers are tests/worker_bot.py started through a real hc template, so everything but the agent is
real: terminals, labels, templates, child events, `hc send`, `hc get`, the doc store, git worktrees.
No LLM calls; free and deterministic. The same flows with real Claude Code sessions are in
test_hco_agents_live.py. Skipped without `hc` (see live_support.py). Takes about a minute.
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path

from live_support import HCDOC, LIVE, SKIP_WHY, LiveHc


def bot(**plan):
    return f"## Goal\nScripted worker.\n\nBOT {json.dumps(plan)}\n"


@unittest.skipUnless(LIVE, SKIP_WHY)
class HcoLive(LiveHc):
    prefix = "hol"

    def test_worker_in_a_worktree_through_check_accept_merge(self):
        run = self.init()
        out = json.loads(self.hco("spawn", "--run", run, "--name", "w1", "--agent", "hcobot", "--worktree",
                                  "--scope", "hello.txt", "--check", "grep -qx hi hello.txt",
                                  "--brief", self.brief("w1", bot(write={"hello.txt": "hi\n"}))).stdout)
        self.assertEqual(out["launch"], "template:hcobot")
        rec = json.loads(self.hc("get", out["address"], "--json").stdout)
        self.assertEqual(rec["labels"]["hco.run"], run)
        self.assertEqual(rec.get("parent"), self.lead)  # started by the lead: hc reports it as its child
        rows = self.wait_reported(run, ["w1"], timeout=90)
        self.assertEqual(rows["w1"]["outbox"], "DONE")
        collect = self.hco("collect", "--run", run).stdout
        self.assertIn("changed in worktree (hco/%s/w1): hello.txt" % run, collect)
        self.assertNotIn("outside its scope", collect)
        self.assertEqual(self.hco("check", "--run", run, "--name", "w1", "--timeout", "60").returncode, 0)
        self.hco("accept", "--run", run, "--name", "w1", "--note", "live test")
        self.hco("merge", "--run", run, "--name", "w1")
        self.assertEqual((self.repo / "hello.txt").read_text(), "hi\n")
        self.hco("cleanup", "--run", run, "--worktrees")
        left = json.loads(self.hc("ls", "--all", "--json", "--label", f"hco.run={run}").stdout)["terminals"]
        self.assertEqual(left, [])

    def test_stray_file_blocks_the_merge(self):
        run = self.init()
        self.hco("spawn", "--run", run, "--name", "w1", "--agent", "hcobot", "--worktree", "--scope", "a.txt",
                 "--brief", self.brief("w1", bot(write={"a.txt": "a\n", "stray.txt": "x\n"})))
        self.wait_reported(run, ["w1"], timeout=90)
        self.assertIn("stray.txt", self.hco("collect", "--run", run).stdout)
        self.assertEqual(self.hco("merge", "--run", run, "--name", "w1", check=False).returncode, 1)
        self.assertFalse((self.repo / "a.txt").exists())


@unittest.skipUnless(LIVE, SKIP_WHY)
class TeamLive(LiveHc):
    prefix = "htl"

    def spawn(self, run, name, plan):
        return json.loads(self.hco("spawn", "--run", run, "--name", name, "--agent", "hcobot",
                                   "--brief", self.brief(name, bot(**plan))).stdout)

    def test_two_members_work_the_list_and_mail_each_other(self):
        run = self.init("--team")
        team = json.loads(self.hcdoc("get", f"sk-{run}-team").stdout)["value"]
        self.assertEqual((team["kind"], team["members"]["lead"]["term"]), ("hco-team", self.lead))
        self.hco("task", "add", "--run", run, "--title", "first")
        self.hco("task", "add", "--run", run, "--title", "second")
        self.hco("task", "add", "--run", run, "--title", "third", "--after", "1")
        a = self.spawn(run, "a", {"claim": True, "mail": [["b", "a says hello"]]})
        b = self.spawn(run, "b", {"claim": True, "wait_mail": 1})
        rows = self.wait_reported(run, ["a", "b"], timeout=120)
        self.assertEqual((rows["a"]["outbox"], rows["b"]["outbox"]), ("DONE", "DONE"))

        tasks = json.loads(self.hco("tasks", "--run", run, "--json").stdout)
        self.assertEqual([t["state"] for t in tasks], ["completed"] * 3)
        self.assertTrue({t["owner"] for t in tasks} <= {"a", "b"})
        done_files = sorted(p.name for p in self.repo.glob("task-*.txt"))
        self.assertEqual(done_files, ["task-1.txt", "task-2.txt", "task-3.txt"])  # shared checkout: both wrote here

        mail = json.loads(self.hco("mail", "--run", run, "--json").stdout)
        self.assertEqual([(m["from"], m["to"], m["text"]) for m in mail], [("a", "b", "a says hello")])
        self.assertEqual(mail[0]["term"], a["address"])  # the real $HC_TERMINAL_ID of a's terminal
        self.assertIsNone(mail[0]["flag"])
        result_b = Path(self.run_json(run)["workers"]["b"]["outbox"], "RESULT.md").read_text()
        self.assertIn("a says hello", result_b)  # b waited for it and read it from its inbox
        self.assertNotIn(b["address"], [m["term"] for m in mail])

        for n in ("a", "b"):
            self.hco("accept", "--run", run, "--name", n, "--note", "live test")
        self.hco("cleanup", "--run", run)
        self.assertTrue((self.rundir(run) / "team.json").is_file())
        for doc in (f"sk-{run}-team", f"sk-{run}-t001", f"sk-{run}-m001"):
            self.assertIsNone(json.loads(self.hcdoc("get", doc).stdout)["value"], doc)

    def test_a_member_cannot_act_under_another_name(self):
        run = self.init("--team")
        self.hco("task", "add", "--run", run, "--title", "only one")
        a = self.spawn(run, "a", {"status": "DONE"})
        self.spawn(run, "b", {"status": "DONE"})
        self.wait_reported(run, ["a", "b"], timeout=90)
        p = subprocess.run([sys.executable, "-I", str(Path(HCDOC).parent / "hcteam.py"), "claim", "--run", run,
                            "--as", "b"], capture_output=True, text=True, timeout=60,
                           env={**self.env, "HC_TERMINAL_ID": a["address"]})
        self.assertEqual(p.returncode, 3, p.stderr)
        self.assertIn("use your own name", p.stderr)

    def hcdoc(self, *args):
        p = subprocess.run([sys.executable, "-I", str(HCDOC), *args], capture_output=True, text=True,
                           env=self.env, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p


if __name__ == "__main__":
    unittest.main()
