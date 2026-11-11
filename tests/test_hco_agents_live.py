"""Live tests with real Claude Code sessions as workers, on a real herdr-core.

Opt-in: they run only with HCO_LIVE_AGENTS=1, `hc` and `claude` on PATH, and Claude Code logged in (or
ANTHROPIC_API_KEY set). They cost real tokens: a few small tasks, typically well under a dollar a run.
Each worker gets up to HCO_LIVE_AGENT_TIMEOUT seconds (default 600); on a timeout the test fails with
the worker's screen, so a dialog the worker is stuck on shows in the output.

They use `hco spawn --trust`, which adds a folder-trust entry for each temporary worker folder to
~/.claude.json (the file Claude Code reads). Run them in a throwaway HOME if you don't want that.
"""
import json
import os
import shutil
import unittest
from pathlib import Path

from live_support import LIVE, LiveHc

AGENTS = LIVE and os.environ.get("HCO_LIVE_AGENTS") == "1" and bool(shutil.which("claude"))
WHY = "needs HCO_LIVE_AGENTS=1, hc and claude (real Claude sessions cost tokens)"
TIMEOUT = int(os.environ.get("HCO_LIVE_AGENT_TIMEOUT", "600"))


@unittest.skipUnless(AGENTS, WHY)
class ClaudeLive(LiveHc):
    prefix = "hal"

    def spawn(self, run, name, brief, *extra):
        out = json.loads(self.hco("spawn", "--run", run, "--name", name, "--agent", "claude", "--trust",
                                  "--brief", self.brief(name, brief), *extra).stdout)
        self.assertEqual(out["launch"], "template:hco-claude", out)
        fm = out.get("first_message") or {}
        self.assertTrue(fm.get("sent"), f"the task did not go in: {fm}\n{self.screen(out['address'])}")
        return out

    def test_one_claude_worker_from_spawn_to_merge(self):
        run = self.init()
        self.spawn(run, "w1", "## Goal\nCreate the file `hello.txt` containing exactly one line: `hi`.\n\n"
                              "## Done means\n`grep -qx hi hello.txt` exits 0.\n",
                   "--worktree", "--scope", "hello.txt", "--check", "grep -qx hi hello.txt")
        rows = self.wait_reported(run, ["w1"], timeout=TIMEOUT)
        self.assertTrue(rows["w1"]["outbox"].startswith("DONE"), rows["w1"])
        self.assertNotIn("outside its scope", self.hco("collect", "--run", run).stdout)
        self.assertEqual(self.hco("check", "--run", run, "--name", "w1", "--timeout", "60").returncode, 0)
        self.hco("accept", "--run", run, "--name", "w1", "--note", "live agent test")
        self.hco("merge", "--run", run, "--name", "w1")
        self.assertEqual((self.repo / "hello.txt").read_text().strip(), "hi")
        self.hco("cleanup", "--run", run, "--worktrees")

    def test_two_claude_members_pass_a_word_by_mail(self):
        """Task 1 (for a): pick a word, write it, mail it to b. Task 2 (for b, after 1): write the word it
        was mailed. The workers share no folder, so the word can only reach b through the mailbox."""
        run = self.init("--team")
        self.hco("task", "add", "--run", run, "--for", "a", "--title",
                 "Pick one random lowercase English word, write it as the only line of word.txt, and mail "
                 "just that word to member b")
        self.hco("task", "add", "--run", run, "--for", "b", "--after", "1", "--title",
                 "Read your inbox: member a mails you one word. Write that word as the only line of echo.txt")
        brief = ("## Goal\nWork through your team's task list until nothing is left for you, then report.\n\n"
                 "## Scope\nMay change: `{f}` only.\n")
        self.spawn(run, "a", brief.format(f="word.txt"), "--worktree", "--scope", "word.txt")
        self.spawn(run, "b", brief.format(f="echo.txt"), "--worktree", "--scope", "echo.txt")
        rows = self.wait_reported(run, ["a", "b"], timeout=TIMEOUT)
        diag = self.hco("collect", "--run", run).stdout
        self.assertTrue(rows["a"]["outbox"].startswith("DONE"), diag)
        self.assertTrue(rows["b"]["outbox"].startswith("DONE"), diag)
        tasks = json.loads(self.hco("tasks", "--run", run, "--json").stdout)
        self.assertEqual([(t["state"], t["owner"]) for t in tasks], [("completed", "a"), ("completed", "b")], diag)
        mail = json.loads(self.hco("mail", "--run", run, "--json").stdout)
        self.assertTrue(any(m["from"] == "a" and m["to"] == "b" and not m["flag"] for m in mail), diag)
        wt = {n: Path(self.run_json(run)["workers"][n]["cwd"]) for n in ("a", "b")}
        word = (wt["a"] / "word.txt").read_text().strip()
        self.assertRegex(word, r"^[a-z]+$")
        self.assertEqual((wt["b"] / "echo.txt").read_text().strip(), word, diag)
        self.hco("cleanup", "--run", run, "--worktrees", "--unmerged")


if __name__ == "__main__":
    unittest.main()
