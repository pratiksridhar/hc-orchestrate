"""Tests for scripts/hco.py against tests/fake_hc.py. Run: python3 -I -m unittest discover -s tests -v

Each test gets a throwaway git repo and fake-hc state; nothing touches the real hc or ~/.claude.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
HCO = Path(os.environ.get("HCO_SCRIPT") or HERE.parent / "scripts" / "hco.py")
FAKE = HERE / "fake_hc.py"
sys.path.insert(0, str(HCO.parent))
import hco  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        for cmd in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"],
                    ["commit", "-q", "--allow-empty", "-m", "base"]):
            subprocess.run(["git", *cmd], cwd=self.repo, check=True, capture_output=True)
        self.env = {**os.environ, "HCO_HC": str(FAKE), "FAKE_HC_STATE": str(Path(self.tmp.name) / "state.json"),
                    "HC_TERMINAL_ID": "t1", "XDG_CONFIG_HOME": str(Path(self.tmp.name) / "cfg"),
                    "FAKE_HC_TEMPLATES": ""}
        FAKE.chmod(0o755)

    def hco(self, *args, env=None, check=True, cwd=None):
        p = subprocess.run([sys.executable, "-I", str(HCO), *args], cwd=cwd or self.repo,
                           env={**self.env, **(env or {})}, capture_output=True, text=True)
        if check and p.returncode != 0:
            self.fail(f"hco {args} exited {p.returncode}\n{p.stdout}\n{p.stderr}")
        return p

    def init_run(self):
        return json.loads(self.hco("init", "--task", "t").stdout)["run"]

    def brief(self, text="## Goal\nDo it.\n"):
        f = Path(self.tmp.name) / "task.md"
        f.write_text(text)
        return str(f)

    def spawn(self, run, name="w1", *extra, **kw):
        return self.hco("spawn", "--run", run, "--name", name, "--agent", "claude", "--brief", self.brief(),
                        *extra, **kw)


class Preflight(Base):
    def test_blocked_outside_hc(self):
        p = self.hco("preflight", env={"HC_TERMINAL_ID": ""}, check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("not inside hc", p.stdout)


class InitSpawn(Base):
    def test_init_excludes_run_dir_from_git(self):
        self.init_run()
        self.assertIn("/.hc-orchestrate/", (self.repo / ".git/info/exclude").read_text())
        st = subprocess.run(["git", "status", "--porcelain"], cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(st.stdout.strip(), "")

    def test_spawn_direct_records_worker_and_brief(self):
        run = self.init_run()
        out = json.loads(self.spawn(run).stdout)
        self.assertEqual(out["launch"], "direct")  # no template installed
        self.assertIn("template hco-claude not installed", out["warnings"][0])
        brief = Path(out["brief"]).read_text()
        self.assertIn("hc send t1", brief)  # reports go to the lead
        self.assertIn("### Goal", brief)  # task headings demoted under '## Your task'
        data = json.loads((self.repo / ".hc-orchestrate" / run / "run.json").read_text())
        self.assertIn("w1", data["workers"])

    def test_spawn_worktree_makes_branch(self):
        run = self.init_run()
        out = json.loads(self.spawn(run, "w1", "--worktree").stdout)
        self.assertEqual(out["branch"], f"hco/{run}/w1")
        self.assertTrue(Path(out["cwd"]).is_dir())

    def test_spawn_rejects_bad_and_duplicate_names(self):
        run = self.init_run()
        self.assertNotEqual(self.spawn(run, "Bad Name", check=False).returncode, 0)
        self.spawn(run, "w1")
        dup = self.spawn(run, "w1", check=False)
        self.assertNotEqual(dup.returncode, 0)
        self.assertIn("already exists", dup.stderr)

    def test_spawn_template_task_not_sent_exits_3(self):
        run = self.init_run()
        p = self.spawn(run, "w1", env={"FAKE_HC_TEMPLATES": "hco-claude", "FAKE_HC_NOT_SENT": "1"}, check=False)
        self.assertEqual(p.returncode, 3)
        self.assertIn("TASK NOT SENT", p.stdout)


class StatusCollectCleanup(Base):
    def test_status_sees_worker_via_get_not_slim_ls(self):
        """Regression: real `hc ls --json` has no name/labels; status must come from `hc get`."""
        run = self.init_run()
        self.spawn(run, "w1")
        rows = json.loads(self.hco("status", "--run", run, "--json").stdout)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["worker"], "w1")
        self.assertEqual(rows[0]["state"], "live")
        self.assertEqual(rows[0]["status"], "working")

    def test_collect_flags_reviewer_that_changed_files(self):
        run = self.init_run()
        out = json.loads(self.hco("spawn", "--run", run, "--name", "rev", "--agent", "claude", "--role", "review",
                                  "--worktree", "--brief", self.brief()).stdout)
        (Path(out["cwd"]) / "oops.txt").write_text("edited")
        text = self.hco("collect", "--run", run).stdout
        self.assertIn("oops.txt", text)
        self.assertIn("review worker but changed files", text)

    def test_cleanup_archives_outbox_and_removes_terminals(self):
        run = self.init_run()
        out = json.loads(self.spawn(run).stdout)
        (Path(out["brief"]).parent / "RESULT.md").write_text("done\n")
        self.hco("cleanup", "--run", run)
        archived = self.repo / ".hc-orchestrate" / run / "workers" / "w1" / "out" / "RESULT.md"
        self.assertTrue(archived.is_file())
        state = json.loads(Path(self.env["FAKE_HC_STATE"]).read_text())
        self.assertEqual(state["terminals"], {})


class Permissions(Base):
    def argv_of(self, out):
        state = json.loads(Path(self.env["FAKE_HC_STATE"]).read_text())
        return next(t["argv"] for t in state["terminals"].values() if t["name"] == out["terminal"])

    def test_bypass_is_default_for_both_agents(self):
        run = self.init_run()
        cl = json.loads(self.spawn(run, "c").stdout)
        cx = json.loads(self.hco("spawn", "--run", run, "--name", "x", "--agent", "codex", "--brief",
                                 self.brief()).stdout)
        self.assertEqual(cl["permissions"], "bypass")
        self.assertIn("--dangerously-skip-permissions", self.argv_of(cl))
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", self.argv_of(cx))
        self.assertIn("--no-daemon", self.argv_of(cx))
        self.assertEqual(self.argv_of(cl)[:2], ["sh", "-c"])  # env scrub wrapper is on by default

    def test_safe_uses_direct_restricted_command_even_with_template(self):
        run = self.init_run()
        out = json.loads(self.spawn(run, "c", "--permissions", "safe", env={"FAKE_HC_TEMPLATES": "hco-claude"}).stdout)
        self.assertEqual(out["launch"], "direct")
        argv = self.argv_of(out)
        self.assertIn("acceptEdits", argv)
        self.assertNotIn("--dangerously-skip-permissions", argv)

    def test_bypass_uses_template_when_installed(self):
        run = self.init_run()
        out = json.loads(self.spawn(run, "c", env={"FAKE_HC_TEMPLATES": "hco-claude"}).stdout)
        self.assertEqual(out["launch"], "template:hco-claude")

    def test_templates_match_direct_bypass_commands(self):
        for agent in ("claude", "codex"):
            line = next(l for l in (HERE.parent / "templates" / f"hco-{agent}.md").read_text().splitlines()
                        if l.startswith("command:"))
            self.assertEqual(line[len("command: "):], hco.SCRUB_SH + " exec " + " ".join(hco.AGENT_ARGV[agent]))


class Scrub(Base):
    def test_no_scrub_runs_agent_directly(self):
        run = self.init_run()
        out = json.loads(self.spawn(run, "c", "--no-scrub").stdout)
        self.assertFalse(out["scrub"])
        state = json.loads(Path(self.env["FAKE_HC_STATE"]).read_text())
        argv = next(t["argv"] for t in state["terminals"].values() if t["name"] == out["terminal"])
        self.assertEqual(argv[0], "claude")

    def test_no_scrub_skips_template(self):
        run = self.init_run()
        out = json.loads(self.spawn(run, "c", "--no-scrub", env={"FAKE_HC_TEMPLATES": "hco-claude"}).stdout)
        self.assertEqual(out["launch"], "direct")

    def test_snippet_unsets_secrets_and_keeps_agent_login_and_normal_vars(self):
        env = {"PATH": os.environ["PATH"], "RALLY_API_KEY": "x", "MAINFRAME_PASSWORD": "x", "MY_TOKEN": "x",
               "GITHUB_TOKEN": "x", "SSH_AUTH_SOCK": "/x", "db_password": "x", "AWS_PROFILE": "x",
               "ANTHROPIC_API_KEY": "keep", "OPENAI_API_KEY": "keep", "CLAUDE_CODE_OAUTH_TOKEN": "keep",
               "HC_TERMINAL_ID": "t1", "EDITOR": "vi", "LANG": "C"}
        p = subprocess.run(["sh", "-c", hco.SCRUB_SH + ' exec env'], env=env, capture_output=True, text=True)
        names = {line.split("=", 1)[0] for line in p.stdout.splitlines() if "=" in line}
        for gone in ("RALLY_API_KEY", "MAINFRAME_PASSWORD", "MY_TOKEN", "GITHUB_TOKEN", "SSH_AUTH_SOCK",
                     "db_password", "AWS_PROFILE"):
            self.assertNotIn(gone, names)
        for kept in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "HC_TERMINAL_ID",
                     "EDITOR", "PATH"):
            self.assertIn(kept, names)


class Trust(Base):
    def setUp(self):
        super().setUp()
        self.cj = Path(self.tmp.name) / "claude.json"
        self.cc = Path(self.tmp.name) / "codex" / "config.toml"
        self.env.update({"HCO_CLAUDE_JSON": str(self.cj), "HCO_CODEX_CONFIG": str(self.cc)})

    def spawn_agent(self, run, agent, *extra, name="w1"):
        return self.hco("spawn", "--run", run, "--name", name, "--agent", agent, "--brief", self.brief(),
                        "--trust", *extra)

    def test_claude_trust_adds_flag_and_keeps_other_data(self):
        self.cj.write_text(json.dumps({"userID": "u", "projects": {"/x": {"hasTrustDialogAccepted": False, "k": 1}}}))
        out = json.loads(self.spawn_agent(self.init_run(), "claude").stdout)
        data = json.loads(self.cj.read_text())
        cwd = out["cwd"]
        self.assertTrue(data["projects"][cwd]["hasTrustDialogAccepted"])
        self.assertEqual(data["projects"]["/x"], {"hasTrustDialogAccepted": False, "k": 1})
        self.assertEqual(data["userID"], "u")
        self.assertIn("trusted", out["trust"])

    def test_claude_trust_creates_missing_file(self):
        out = json.loads(self.spawn_agent(self.init_run(), "claude").stdout)
        self.assertTrue(json.loads(self.cj.read_text())["projects"][out["cwd"]]["hasTrustDialogAccepted"])

    def test_codex_trust_appends_section_once(self):
        import tomllib
        self.cc.parent.mkdir(parents=True)
        self.cc.write_text('model = "m"\n[projects."/a"]\ntrust_level = "trusted"')  # no trailing newline
        run = self.init_run()
        out = json.loads(self.spawn_agent(run, "codex").stdout)
        cfg = tomllib.loads(self.cc.read_text())
        self.assertEqual(cfg["projects"][out["cwd"]]["trust_level"], "trusted")
        self.assertEqual(cfg["projects"]["/a"]["trust_level"], "trusted")
        self.assertEqual(cfg["model"], "m")
        before = self.cc.read_text()
        out2 = json.loads(self.spawn_agent(run, "codex", name="w2").stdout)
        self.assertEqual(self.cc.read_text(), before)  # already trusted: no change
        self.assertIn("already trusted", out2["trust"])

    def test_codex_untrusted_entry_is_left_alone(self):
        import tomllib
        self.cc.parent.mkdir(parents=True)
        run = self.init_run()
        cwd = str(self.repo.resolve())
        self.cc.write_text(f'[projects.{json.dumps(cwd)}]\ntrust_level = "untrusted"\n')
        out = json.loads(self.spawn_agent(run, "codex").stdout)
        self.assertEqual(tomllib.loads(self.cc.read_text())["projects"][cwd]["trust_level"], "untrusted")
        self.assertIn("left unchanged", out["trust"])

    def test_trust_covers_worktree_path(self):
        out = json.loads(self.spawn_agent(self.init_run(), "claude", "--worktree").stdout)
        self.assertIn("/wt/w1", out["cwd"])
        self.assertIn(out["cwd"], json.loads(self.cj.read_text())["projects"])

    def test_no_flag_touches_nothing(self):
        self.spawn(self.init_run())
        self.assertFalse(self.cj.exists())
        self.assertFalse(self.cc.exists())

    def test_trust_rejected_with_on(self):
        p = self.hco("spawn", "--run", self.init_run(), "--name", "w1", "--agent", "claude", "--brief", self.brief(),
                     "--trust", "--on", "vps", "--cwd", "/srv", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("--trust", p.stderr)

    def test_bad_claude_json_warns_but_still_spawns(self):
        self.cj.write_text("{not json")
        out = json.loads(self.spawn_agent(self.init_run(), "claude").stdout)
        self.assertTrue(any("--trust failed" in w for w in out["warnings"]))
        self.assertEqual(self.cj.read_text(), "{not json")


class Merge(Base):
    def wt_worker(self, run, name, files):
        out = json.loads(self.spawn(run, name, "--worktree").stdout)
        for fname, text in files.items():
            (Path(out["cwd"]) / fname).write_text(text)
        return out

    def test_merges_two_workers_in_order(self):
        run = self.init_run()
        self.wt_worker(run, "a", {"a.txt": "a"})
        self.wt_worker(run, "b", {"b.txt": "b"})
        out = self.hco("merge", "--run", run).stdout
        self.assertIn("merged a", out)
        self.assertIn("merged b", out)
        self.assertTrue((self.repo / "a.txt").is_file() and (self.repo / "b.txt").is_file())
        self.assertFalse((self.repo / ".hc-orchestrate" / run / "out").exists())
        tracked = subprocess.run(["git", "ls-files"], cwd=self.repo, capture_output=True, text=True).stdout
        self.assertNotIn(".hc-orchestrate", tracked)  # outbox must never be committed

    def test_conflict_aborts_and_leaves_root_clean(self):
        run = self.init_run()
        self.wt_worker(run, "a", {"same.txt": "from a"})
        self.wt_worker(run, "b", {"same.txt": "from b"})
        p = self.hco("merge", "--run", run, check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("CONFLICT merging b", p.stdout)
        self.assertEqual((self.repo / "same.txt").read_text(), "from a")  # a merged, b aborted
        st = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=self.repo,
                            capture_output=True, text=True).stdout
        self.assertEqual(st.strip(), "")

    def test_refuses_dirty_root(self):
        run = self.init_run()
        self.wt_worker(run, "a", {"a.txt": "a"})
        (self.repo / "tracked.txt").write_text("x")
        subprocess.run(["git", "add", "tracked.txt"], cwd=self.repo, check=True)
        p = self.hco("merge", "--run", run, check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("uncommitted changes", p.stderr)

    def test_name_filter_and_unknown_name(self):
        run = self.init_run()
        self.wt_worker(run, "a", {"a.txt": "a"})
        self.wt_worker(run, "b", {"b.txt": "b"})
        self.hco("merge", "--run", run, "--name", "b")
        self.assertTrue((self.repo / "b.txt").is_file())
        self.assertFalse((self.repo / "a.txt").exists())
        self.assertNotEqual(self.hco("merge", "--run", run, "--name", "zzz", check=False).returncode, 0)


class Units(unittest.TestCase):
    def test_demote_headings_skips_code_fences(self):
        md = "# Top\ntext\n```\n# not a heading\n```\n## Sub"
        self.assertEqual(hco.demote_headings(md, 3), "### Top\ntext\n```\n# not a heading\n```\n#### Sub")


if __name__ == "__main__":
    unittest.main()
