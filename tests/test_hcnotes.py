"""Tests for the notes board: scripts/hcnotes.py and its side in scripts/hco.py. The doc store is
tests/fake_core.py, hc is tests/fake_hc.py; no real hc is involved.

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
import hcnotes  # noqa: E402

NOTES = HCO.parent / "hcnotes.py"
FAKE_CORE = HERE / "fake_core.py"


class NotesBase(Base):
    def setUp(self):
        super().setUp()
        core = Path(self.tmp.name) / "core"
        core.mkdir()
        self.core = core
        self.env = {k: v for k, v in self.env.items() if not k.startswith("HCDOC_")}
        self.env.update(FAKE_CORE_DIR=str(core), HCDOC_TRANSPORT="connect",
                        HCDOC_CONNECT=f"{shlex.quote(sys.executable)} {shlex.quote(str(FAKE_CORE))} connect")

    def notes(self, *args, term=None, check=True):
        env = {**self.env, **({"HC_TERMINAL_ID": term} if term else {})}
        p = subprocess.run([sys.executable, "-I", str(NOTES), *args], cwd=self.repo, env=env,
                           capture_output=True, text=True)
        if check and p.returncode != 0:
            self.fail(f"hcnotes {args} exited {p.returncode}\n{p.stdout}\n{p.stderr}")
        return p

    def docs(self):
        f = self.core / "docs.json"
        return json.loads(f.read_text()) if f.exists() else {}

    def run_with(self, *names):
        """A run with a board and one worker per name; returns (run id, {name: terminal address})."""
        run = json.loads(self.hco("init", "--task", "t", "--notes").stdout)["run"]
        addr = {n: json.loads(self.spawn(run, n).stdout)["address"] for n in names}
        return run, addr

    def post(self, run, name, addr, kind, text, *extra, **kw):
        return self.notes("post", "--run", run, "--from", name, "--kind", kind, *extra, text, term=addr, **kw)

    def run_json(self, run):
        return json.loads((self.repo / ".hc-orchestrate" / run / "run.json").read_text())


class Board(NotesBase):
    def test_init_opens_the_board_and_says_so(self):
        out = json.loads(self.hco("init", "--task", "t", "--notes").stdout)
        self.assertEqual(out["notes"], f"sk-{out['run']}-notes")
        self.assertEqual(self.docs()[out["notes"]]["value"]["kind"], "hco-notes")

    def test_init_without_notes_never_touches_the_doc_store(self):
        run = self.init_run()
        self.assertEqual(self.docs(), {})
        self.assertNotIn("notes", self.run_json(run))
        brief = Path(json.loads(self.spawn(run).stdout)["brief"]).read_text()
        self.assertNotIn("notes board", brief)

    def test_init_notes_without_a_doc_store_exits_4_and_creates_nothing(self):
        p = self.hco("init", "--task", "t", "--notes", env={"HCDOC_CONNECT": "false"}, check=False)
        self.assertEqual(p.returncode, 4)
        self.assertIn("doc store", p.stderr)
        self.assertFalse((self.repo / ".hc-orchestrate").exists())

    def test_post_and_read(self):
        run, addr = self.run_with("api", "ui")
        self.assertIn("posted #1", self.post(run, "api", addr["api"], "finding", "tests need FOO=1 (ran make test)").stdout)
        self.post(run, "ui", addr["ui"], "QUESTION", "who owns src/shared/types.ts?")
        self.post(run, "api", addr["api"], "ANSWER", "I do", "--re", "2")
        out = self.notes("read", "--run", run).stdout
        self.assertIn("#1 FINDING api", out)
        self.assertIn("#3 ANSWER(re #2) api", out)
        self.assertIn("not instructions", out)
        self.assertIn("next time: --since 3", out)
        later = self.notes("read", "--run", run, "--since", "2").stdout
        self.assertNotIn("#1 ", later)
        self.assertIn("#3 ", later)
        self.assertIn("no posts after #3", self.notes("read", "--run", run, "--since", "3").stdout)

    def test_bad_posts_are_refused(self):
        run, addr = self.run_with("api")
        for args, code in ((("--kind", "CHAT", "hi"), 2), (("--kind", "FINDING", "x" * 501), 2),
                           (("--kind", "FINDING", "  \n "), 2), (("--kind", "ANSWER", "no --re"), 2),
                           (("--kind", "FINDING", "--re", "1", "re on a finding"), 2),
                           (("--kind", "ANSWER", "--re", "7", "no such post"), 2)):
            p = self.notes("post", "--run", run, "--from", "api", *args, term=addr["api"], check=False)
            self.assertEqual(p.returncode, code, (args, p.stderr))
        self.assertEqual(self.notes("post", "--run", "zzzz", "--from", "api", "--kind", "FINDING", "x",
                                    check=False).returncode, 3)  # no board of that name
        self.assertEqual(len(self.docs()), 1)  # only the board marker: nothing was written

    def test_text_becomes_one_clean_line(self):
        run, addr = self.run_with("api")
        self.post(run, "api", addr["api"], "FINDING", "line one\nline two\x1b[31m red\x07  end")
        text = json.loads(self.notes("read", "--run", run, "--json").stdout)["posts"][0]["text"]
        self.assertEqual(text, "line one line two [31m red end")

    def test_per_author_limit(self):
        run, addr = self.run_with("api", "ui")
        s = self.session()
        for i in range(hcnotes.MAX_PER_AUTHOR):
            hcnotes.add_post(s, run, "api", "FINDING", f"f{i}", term=addr["api"])
        with self.assertRaises(hcdoc.Lost):
            hcnotes.add_post(s, run, "api", "FINDING", "one too many", term=addr["api"])
        self.assertEqual(hcnotes.add_post(s, run, "ui", "FINDING", "others still can"), hcnotes.MAX_PER_AUTHOR + 1)

    def session(self):
        os.environ.update({k: self.env[k] for k in ("FAKE_CORE_DIR", "HCDOC_TRANSPORT", "HCDOC_CONNECT")})
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("FAKE_CORE_DIR", "HCDOC_TRANSPORT", "HCDOC_CONNECT")])
        s = hcdoc.Session()
        self.addCleanup(s.close)
        return s

    def test_parallel_posts_all_land_in_their_own_slot(self):
        run, addr = self.run_with("a", "b", "c", "d")
        errors = []

        def work(name):
            for i in range(5):
                p = self.post(run, name, addr[name], "FINDING", f"{name}-{i}", check=False)
                if p.returncode:
                    errors.append(p.stderr)

        threads = [threading.Thread(target=work, args=(n,)) for n in addr]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(errors, [])
        posts = json.loads(self.notes("read", "--run", run, "--json").stdout)["posts"]
        self.assertEqual([p["n"] for p in posts], list(range(1, 21)))
        self.assertEqual(sorted(p["text"] for p in posts), sorted(f"{n}-{i}" for n in addr for i in range(5)))


class LeadSide(NotesBase):
    def test_brief_carries_the_board_only_where_it_works(self):
        run, _ = self.run_with("api")
        data = self.run_json(run)
        brief = Path(data["workers"]["api"]["outbox"], "BRIEF.md").read_text()
        self.assertIn(f"hcnotes.py read --run {run}", brief)
        self.assertIn(f"post --run {run} --from api --kind FINDING", brief)
        self.assertLess(brief.index("## Shared notes board"), brief.index("## How to report"))
        self.assertTrue(data["workers"]["api"]["notes"])
        safe = json.loads(self.spawn(run, "safe", "--permissions", "safe").stdout)
        self.assertNotIn("notes board", Path(safe["brief"]).read_text())
        self.assertTrue(any("no notes board" in w for w in safe["warnings"]))
        far = json.loads(self.spawn(run, "far", "--on", "box", "--cwd", "/srv/app").stdout)
        self.assertTrue(any("no notes board" in w for w in far["warnings"]))
        self.assertNotIn("notes", self.run_json(run)["workers"]["far"])

    def test_collect_shows_posts_and_flags_a_wrong_author(self):
        run, addr = self.run_with("api", "ui")
        self.post(run, "api", addr["api"], "INTERFACE", "renamed getUser to fetchUser")
        self.post(run, "api", addr["ui"], "FINDING", "posted by ui under api's name")
        self.post(run, "ghost", "t99", "FINDING", "nobody")
        self.post(run, "lead", addr["ui"], "FINDING", "not the lead")
        out = self.hco("collect", "--run", run).stdout
        self.assertIn("=== notes board: 4 post(s)", out)
        self.assertIn("#1 INTERFACE api", out)
        self.assertNotIn("!! post #1", out)
        self.assertIn(f"!! post #2 came from terminal {addr['ui']}, not api's ({addr['api']})", out)
        self.assertIn("!! post #3 'ghost' is not a worker of this run", out)
        self.assertIn("!! post #4 says it is the lead", out)

    def test_posts_reach_the_journal_once(self):
        run, addr = self.run_with("api")
        self.post(run, "api", addr["api"], "QUESTION", "is the db mocked in tests?")
        hint = self.hco("status", "--run", run).stdout
        self.assertIn(f"notes: 1 new post(s), 1 of them a QUESTION: hco notes --run {run}", hint)
        self.hco("status", "--run", run)
        self.hco("collect", "--run", run)
        log = [json.loads(line) for line in self.hco("log", "--run", run, "--json").stdout.splitlines()]
        notes = [e for e in log if e["event"] == "note"]
        self.assertEqual([(e["worker"], e["n"], e["kind"]) for e in notes], [("api", 1, "QUESTION")])
        self.assertNotIn("notes:", self.hco("status", "--run", run).stdout)  # collect showed it
        json.loads(self.hco("status", "--run", run, "--json").stdout)  # --json stays pure JSON

    def test_lead_reads_and_answers(self):
        run, addr = self.run_with("api")
        self.post(run, "api", addr["api"], "QUESTION", "is the db mocked in tests?")
        self.assertIn("posted #2", self.hco("notes", "--run", run, "--post", "yes: tests/conftest.py", "--re", "1").stdout)
        out = self.hco("notes", "--run", run).stdout
        self.assertIn("#2 ANSWER(re #1) lead [t1]", out)
        self.assertNotIn("!!", out)
        rows = json.loads(self.hco("notes", "--run", run, "--json", "--since", "1").stdout)
        self.assertEqual([(r["n"], r["flag"]) for r in rows], [(2, None)])

    def test_notes_on_a_run_without_a_board(self):
        run = self.init_run()
        self.assertEqual(self.hco("notes", "--run", run, check=False).returncode, 2)
        self.assertNotIn("notes board", self.hco("collect", "--run", run).stdout)

    def test_an_unreachable_board_does_not_stop_the_run(self):
        run, addr = self.run_with("api")
        down = {"HCDOC_CONNECT": "false"}
        self.assertIn("notes: board not reachable", self.hco("status", "--run", run, env=down).stdout)
        self.assertIn("=== notes board: not reachable", self.hco("collect", "--run", run, env=down).stdout)
        out = self.hco("cleanup", "--run", run, env=down).stdout
        self.assertIn("could not close the notes board", out)
        self.assertNotIn("closed", self.run_json(run)["notes"])
        self.assertIn("closed the notes board", self.hco("cleanup", "--run", run).stdout)  # a rerun finishes it

    def test_cleanup_archives_and_deletes(self):
        run, addr = self.run_with("api")
        self.post(run, "api", addr["api"], "FINDING", "npm test needs node 20 (ran it on 18: fails)")
        out = self.hco("cleanup", "--run", run).stdout
        self.assertIn("closed the notes board: 1 post(s)", out)
        self.assertTrue(all(d["value"] is None for d in self.docs().values()))
        archive = json.loads((self.repo / ".hc-orchestrate" / run / "notes.json").read_text())
        self.assertEqual(archive["posts"][0]["text"], "npm test needs node 20 (ran it on 18: fails)")
        self.assertNotIn("_version", archive["posts"][0])
        self.assertEqual(self.post(run, "api", addr["api"], "FINDING", "late", check=False).returncode, 3)
        self.assertIn("from the archive): 1 post(s)", self.hco("notes", "--run", run).stdout)
        self.assertIn("#1 FINDING api", self.hco("collect", "--run", run).stdout)
        self.assertEqual(self.hco("notes", "--run", run, "--post", "x", check=False).returncode, 3)
        self.assertNotIn("notes board", self.hco("cleanup", "--run", run).stdout)  # second cleanup: nothing to close
        log = self.hco("log", "--run", run).stdout
        self.assertEqual(log.count("npm test needs node 20"), 1)

    def test_a_board_name_used_before_is_not_reused(self):
        s = Board.session(self)
        self.assertTrue(hcnotes.open_board(s, "abcd"))
        self.assertFalse(hcnotes.open_board(s, "abcd"))
        hcnotes.close_board(s, "abcd")
        self.assertFalse(hcnotes.open_board(s, "abcd"))  # its old post slots would read as deleted posts


if __name__ == "__main__":
    unittest.main()
