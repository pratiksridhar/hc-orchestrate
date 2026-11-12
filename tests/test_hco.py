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

import gitenv  # noqa: F401  (git without the developer's global config)

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

    def test_check_artifacts_are_not_committed(self):
        # Live failure on hc 0.1.18: both workers ran their check, each left the same .pyc behind,
        # merge committed it and the second merge conflicted although no source file was shared.
        run = self.init_run()
        for name, fname in (("a", "a.py"), ("b", "b.py")):
            out = self.wt_worker(run, name, {fname: "x = 1\n"})
            for junk in ("__pycache__/calc.cpython-313.pyc", ".pytest_cache/v/x", "node_modules/m/i.js",
                         ".DS_Store"):
                p = Path(out["cwd"]) / junk
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(name.encode() * 8)
        self.assertIn("changed in worktree (hco/" + run + "/a): a.py\n", self.hco("collect", "--run", run).stdout)
        out = self.hco("merge", "--run", run).stdout
        self.assertIn("merged b", out)
        tracked = subprocess.run(["git", "ls-files"], cwd=self.repo, capture_output=True, text=True).stdout.split()
        self.assertEqual(sorted(tracked), ["a.py", "b.py"])

    def test_init_exclude_is_idempotent(self):
        self.init_run()
        self.init_run()
        lines = (self.repo / ".git/info/exclude").read_text().splitlines()
        self.assertEqual(lines.count("/.hc-orchestrate/"), 1)
        self.assertEqual(lines.count("__pycache__/"), 1)

    def test_dry_run_lists_files_and_changes_nothing(self):
        run = self.init_run()
        self.wt_worker(run, "a", {"a.txt": "a"})
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo, capture_output=True, text=True).stdout
        out = self.hco("merge", "--run", run, "--dry-run").stdout
        self.assertIn("a (hco/" + run + "/a): 1 file(s), 0 commit(s) ahead", out)
        self.assertIn("  a.txt", out)
        self.assertIn("dry run", out)
        self.assertEqual(subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo, capture_output=True,
                                        text=True).stdout, head)
        self.assertFalse((self.repo / "a.txt").exists())
        self.assertIn("merged a", self.hco("merge", "--run", run).stdout)  # still merges afterwards

    def test_name_filter_and_unknown_name(self):
        run = self.init_run()
        self.wt_worker(run, "a", {"a.txt": "a"})
        self.wt_worker(run, "b", {"b.txt": "b"})
        self.hco("merge", "--run", run, "--name", "b")
        self.assertTrue((self.repo / "b.txt").is_file())
        self.assertFalse((self.repo / "a.txt").exists())
        self.assertNotEqual(self.hco("merge", "--run", run, "--name", "zzz", check=False).returncode, 0)


class Remote(Base):
    """--on workers. The fake hc runs `exec` and `file` locally, so a temp repo stands in for the
    other machine's checkout."""

    def setUp(self):
        super().setUp()
        self.far = Path(self.tmp.name) / "far"
        self.far.mkdir()
        for cmd in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
            subprocess.run(["git", *cmd], cwd=self.far, check=True, capture_output=True)
        (self.far / "a.txt").write_text("a\n")
        (self.far / "keep.txt").write_text("k\n")
        subprocess.run(["git", "add", "-A"], cwd=self.far, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=self.far, check=True)
        (self.far / "keep.txt").write_text("dirty before the worker\n")  # pre-existing change: not the worker's

    def spawn_far(self, run, name="w1", role="implement", cwd=None, *extra, **kw):
        return json.loads(self.hco("spawn", "--run", run, "--name", name, "--agent", "claude", "--role", role,
                                   "--on", "box", "--cwd", str(cwd or self.far), "--brief", self.brief(), *extra,
                                   **kw).stdout)

    def collect(self, run):
        return self.hco("collect", "--run", run).stdout

    def test_spawn_excludes_outbox_and_artifacts_there(self):
        out = self.spawn_far(self.init_run(), "w1", "implement", None, "--scope", "a.txt")
        self.assertEqual(out["warnings"], [])
        ex = (self.far / ".git/info/exclude").read_text().splitlines()
        self.assertIn("/.hc-orchestrate/", ex)
        self.assertIn("__pycache__/", ex)
        st = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=self.far,
                            capture_output=True, text=True).stdout
        self.assertEqual(st.strip(), "M keep.txt")  # the outbox (with BRIEF.md) is not listed

    def test_collect_lists_only_what_changed_since_spawn(self):
        run = self.init_run()
        self.spawn_far(run)
        self.assertIn("since this worker started (by it or another worker there): nothing", self.collect(run))
        (self.far / "a.txt").write_text("changed\n")
        (self.far / "new.txt").write_text("n\n")
        (self.far / "__pycache__").mkdir()
        (self.far / "__pycache__" / "m.pyc").write_bytes(b"x")
        out = self.collect(run)
        self.assertIn("worker there): a.txt, new.txt\n", out)  # not keep.txt, not the .pyc, not the outbox

    def test_collect_sees_commits_and_reverts(self):
        run = self.init_run()
        self.spawn_far(run)
        (self.far / "a.txt").write_text("committed by worker\n")
        subprocess.run(["git", "commit", "-q", "-am", "w"], cwd=self.far, check=True)
        out = self.collect(run)
        # keep.txt's earlier change went into the commit, but its content is what it was at spawn
        self.assertIn("worker there): a.txt\n", out)
        subprocess.run(["git", "reset", "-q", "--hard", "HEAD~1"], cwd=self.far, check=True)
        (self.far / "keep.txt").write_text("dirty before the worker\n")
        self.assertIn("worker there): nothing", self.collect(run))

    def test_reviewer_that_changes_files_is_flagged(self):
        run = self.init_run()
        self.spawn_far(run, "rev", role="review")
        (self.far / "a.txt").write_text("oops\n")
        self.assertIn("!! rev is a review worker and files changed", self.collect(run))

    def test_not_a_git_repo_warns(self):
        plain = Path(self.tmp.name) / "plain"
        plain.mkdir()
        out = self.spawn_far(self.init_run(), cwd=plain)
        self.assertIn("not a git repo", out["warnings"][0])

    def test_cleanup_archives_then_removes_remote_outbox(self):
        run = self.init_run()
        out = self.spawn_far(run)
        outbox = Path(out["brief"]).parent
        (outbox / "STATUS").write_text("DONE\n")
        (outbox / "RESULT.md").write_text("# r\n")
        res = self.hco("cleanup", "--run", run).stdout
        self.assertIn("archived and removed w1's outbox on box", res)
        self.assertFalse((self.far / ".hc-orchestrate").exists())  # empty parents removed too
        arch = self.repo / ".hc-orchestrate" / run / "workers" / "w1"
        self.assertEqual((arch / "out" / "STATUS").read_text(), "DONE\n")
        self.assertTrue((arch / "BRIEF.md").is_file())

    def test_scope_is_checked_on_the_remote_checkout(self):
        run = self.init_run()
        self.spawn_far(run, "w1", "implement", None, "--scope", "a.txt")
        (self.far / "a.txt").write_text("in scope\n")
        (self.far / "new.txt").write_text("not in scope\n")
        self.assertIn("!! w1 changed files outside its scope (a.txt): new.txt\n", self.collect(run))

    def test_run_spawned_by_older_hco_still_collects(self):
        """run.json from before the scope change kept the remote snapshot under `remote_git`."""
        run = self.init_run()
        self.spawn_far(run)
        rj = self.repo / ".hc-orchestrate" / run / "run.json"
        data = json.loads(rj.read_text())
        w = data["workers"]["w1"]
        w["remote_git"], w["fingerprint"] = w.pop("snapshot"), None
        del w["scope"]
        rj.write_text(json.dumps(data))
        (self.far / "a.txt").write_text("changed\n")
        self.assertIn("worker there): a.txt\n", self.collect(run))

    def test_cleanup_keeps_remote_outbox_when_unreachable(self):
        run = self.init_run()
        out = self.spawn_far(run)
        (Path(out["brief"]).parent / "STATUS").write_text("DONE\n")
        res = self.hco("cleanup", "--run", run, env={"FAKE_HC_EXEC_DOWN": "1"}).stdout
        self.assertIn("kept remote outbox of w1 on box", res)
        self.assertTrue((Path(out["brief"]).parent / "STATUS").is_file())


class Scope(Base):
    """--scope: the files a writer may change, checked by collect and enforced by merge."""

    def git(self, *args, cwd=None):
        return subprocess.run(["git", *args], cwd=cwd or self.repo, check=True, capture_output=True, text=True)

    def run_json(self, run):
        return json.loads((self.repo / ".hc-orchestrate" / run / "run.json").read_text())

    def wt(self, run, name, scope, files):
        out = json.loads(self.spawn(run, name, "--worktree", *(["--scope", *scope] if scope else [])).stdout)
        for fname, text in files.items():
            p = Path(out["cwd"]) / fname
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        return out

    def head(self):
        return self.git("rev-parse", "HEAD").stdout

    def test_spawn_records_scope_and_puts_it_in_the_brief(self):
        run = self.init_run()
        out = json.loads(self.spawn(run, "w1", "--worktree", "--scope", "./src/auth/**", "README.md",
                                    "--scope", "docs/").stdout)
        self.assertEqual(out["scope"], ["src/auth/**", "README.md", "docs/"])
        self.assertFalse(any("--scope" in w for w in out["warnings"]))
        self.assertEqual(self.run_json(run)["workers"]["w1"]["scope"], ["src/auth/**", "README.md", "docs/"])
        brief = Path(out["brief"]).read_text()
        self.assertIn(f"relative to `{out['cwd']}`", brief)
        self.assertIn("  - `src/auth/**`\n  - `README.md`\n  - `docs/`\n", brief)
        self.assertIn("BLOCKED: needs <path> (outside my scope)", brief)

    def test_writer_without_scope_gets_a_warning_and_the_old_brief_line(self):
        run = self.init_run()
        out = json.loads(self.spawn(run).stdout)
        self.assertTrue(any(w.startswith("no --scope") for w in out["warnings"]))
        self.assertIn("Change only what the Scope section of your task allows.", Path(out["brief"]).read_text())
        self.assertIsNone(self.run_json(run)["workers"]["w1"]["scope"])

    def test_reviewer_gets_no_scope_warning_and_rejects_scope(self):
        run = self.init_run()
        rev = lambda name, *x: self.hco("spawn", "--run", run, "--name", name, "--agent", "claude", "--role",
                                         "review", "--brief", self.brief(), *x, check=False)
        self.assertFalse(any("--scope" in w for w in json.loads(rev("r1").stdout)["warnings"]))
        p = rev("r2", "--scope", "src/**")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("may change none", p.stderr)

    def test_bad_patterns_are_rejected_before_anything_starts(self):
        run = self.init_run()
        for bad, why in (("/abs/src/**", "relative to the repo root"), ("src/../etc", "'..'"),
                         ("", "empty"), ("./", "empty")):
            p = self.spawn(run, "w1", "--worktree", "--scope", bad, check=False)
            self.assertNotEqual(p.returncode, 0, bad)
            self.assertIn(why, p.stderr)
        self.assertEqual(self.run_json(run)["workers"], {})
        self.assertFalse((self.repo / ".hc-orchestrate" / run / "wt").exists())  # no worktree was made

    def test_collect_flags_files_outside_scope(self):
        run = self.init_run()
        out = self.wt(run, "w1", ["src/**"], {"src/a.py": "a"})
        self.assertIn("scope ok (src/**)", self.hco("collect", "--run", run).stdout)
        (Path(out["cwd"]) / "notes.txt").write_text("scratch")
        self.assertIn("!! w1 changed files outside its scope (src/**): notes.txt\n",
                      self.hco("collect", "--run", run).stdout)

    def test_collect_without_scope_asks_for_a_manual_check(self):
        run = self.init_run()
        self.wt(run, "w1", None, {"a.py": "a"})
        self.assertIn("scope: none set at spawn", self.hco("collect", "--run", run).stdout)

    def test_merge_refuses_out_of_scope_and_changes_nothing(self):
        run = self.init_run()
        self.wt(run, "a", ["a/**"], {"a/x.py": "a"})
        b = self.wt(run, "b", ["b/**"], {"b/x.py": "b", "stray.log": "junk"})
        before = self.head()
        p = self.hco("merge", "--run", run, check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("OUT OF SCOPE b (scope b/**): stray.log", p.stdout)
        self.assertNotIn("OUT OF SCOPE a", p.stdout)
        self.assertEqual(self.head(), before)  # not even the in-scope worker a was merged
        self.assertFalse((self.repo / "a").exists())
        self.assertEqual(self.git("log", "--oneline", "-1", f"hco/{run}/a").stdout,
                         self.git("log", "--oneline", "-1", before.strip()).stdout)  # nothing committed in a either
        (Path(b["cwd"]) / "stray.log").unlink()  # the lead removes the stray file, then merges
        out = self.hco("merge", "--run", run).stdout
        self.assertIn("merged a", out)
        self.assertIn("merged b", out)
        self.assertEqual(sorted(self.git("ls-files").stdout.split()), ["a/x.py", "b/x.py"])

    def test_merge_ignore_scope_merges_and_says_so(self):
        run = self.init_run()
        self.wt(run, "a", ["src/**"], {"src/x.py": "x", "package-lock.json": "{}"})
        out = self.hco("merge", "--run", run, "--ignore-scope").stdout
        self.assertIn("merging a despite files outside its scope (--ignore-scope): package-lock.json", out)
        self.assertIn("merged a", out)
        self.assertTrue((self.repo / "package-lock.json").is_file())

    def test_dry_run_marks_out_of_scope_files(self):
        run = self.init_run()
        self.wt(run, "a", ["src/**"], {"src/x.py": "x", "notes.md": "n"})
        self.wt(run, "b", None, {"b.txt": "b"})
        out = self.hco("merge", "--run", run, "--dry-run").stdout
        self.assertIn(f"a (hco/{run}/a): 2 file(s), 0 commit(s) ahead of", out)
        self.assertIn("; scope src/**\n", out)
        self.assertIn("  !! notes.md   (outside scope)\n", out)
        self.assertIn("  src/x.py\n", out)
        self.assertIn("; no scope set\n", out)
        self.assertIn("note: 1 file(s) outside their worker's scope; a real merge would refuse", out)

    def test_rename_out_of_scope_is_flagged_by_its_old_path(self):
        (self.repo / "legacy").mkdir()
        (self.repo / "legacy" / "old.py").write_text("x = 1\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "legacy")
        run = self.init_run()
        # Moving legacy/old.py into src/ deletes a file outside the scope. With rename detection git only
        # shows the new path, so the deletion went unseen; both a staged and a committed rename must show it.
        for name, commit in (("staged", False), ("committed", True)):
            w = self.wt(run, name, ["src/**"], {})
            (Path(w["cwd"]) / "src").mkdir()
            self.git("mv", "legacy/old.py", "src/new.py", cwd=w["cwd"])
            if commit:
                self.git("commit", "-q", "-m", "worker committed a rename", cwd=w["cwd"])
        out = self.hco("collect", "--run", run).stdout
        self.assertIn("!! staged changed files outside its scope (src/**): legacy/old.py\n", out)
        self.assertIn("!! committed changed files outside its scope (src/**): legacy/old.py\n", out)
        self.assertEqual(out.count("legacy/old.py, src/new.py"), 2)  # both paths in each file list

    def test_names_with_spaces_and_unicode_match_their_scope(self):
        run = self.init_run()
        self.wt(run, "w1", ["docs/**"], {"docs/naïve file (1).md": "x"})
        out = self.hco("collect", "--run", run).stdout
        self.assertIn("docs/naïve file (1).md", out)
        self.assertIn("scope ok (docs/**)", out)

    def test_shared_checkout_lists_its_files_and_checks_scope(self):
        (self.repo / "before.txt").write_text("dirty before the worker\n")  # not this worker's change
        run = self.init_run()
        out = json.loads(self.spawn(run, "w1", "--scope", "src/**").stdout)
        self.assertEqual(self.run_json(run)["workers"]["w1"]["snapshot"]["top"], str(self.repo.resolve()))
        self.assertIn(f"relative to `{self.repo.resolve()}`", Path(out["brief"]).read_text())
        (self.repo / "src").mkdir()
        (self.repo / "src" / "a.py").write_text("a")
        (self.repo / "other.txt").write_text("o")
        text = self.hco("collect", "--run", run).stdout
        self.assertIn(f"changed in {self.repo.resolve()} since this worker started (by it or another worker "
                      "there): other.txt, src/a.py\n", text)
        self.assertIn("!! w1 changed files outside its scope (src/**): other.txt\n", text)

    def test_scope_add_widens_a_blocked_worker_and_merge_then_accepts(self):
        run = self.init_run()
        self.wt(run, "w1", ["src/**"], {"src/a.py": "a", "tests/test_a.py": "t"})
        self.assertEqual(self.hco("merge", "--run", run, check=False).returncode, 1)
        out = self.hco("scope", "--run", run, "--name", "w1", "--add", "tests/test_a.py", "src/**").stdout
        self.assertIn("added to w1's scope: tests/test_a.py\n", out)  # src/** was already there
        self.assertIn(f'hc send {run}-w1 "You may now also change: tests/test_a.py.', out)
        self.assertEqual(self.run_json(run)["workers"]["w1"]["scope"], ["src/**", "tests/test_a.py"])
        self.assertIn("scope ok (src/**, tests/test_a.py)", self.hco("collect", "--run", run).stdout)
        self.assertIn("merged w1", self.hco("merge", "--run", run).stdout)

    def test_scope_shows_and_refuses_reviewers_and_strangers(self):
        run = self.init_run()
        self.spawn(run, "w1")
        self.assertIn("scope of w1: (none set)", self.hco("scope", "--run", run, "--name", "w1").stdout)
        self.hco("spawn", "--run", run, "--name", "rev", "--agent", "claude", "--role", "review", "--brief",
                 self.brief())
        p = self.hco("scope", "--run", run, "--name", "rev", "--add", "x", check=False)
        self.assertIn("may change no files", p.stderr)
        self.assertNotEqual(self.hco("scope", "--run", run, "--name", "zzz", check=False).returncode, 0)

    def test_shared_checkout_in_a_subfolder_uses_repo_root_paths(self):
        sub = self.repo / "pkg"
        sub.mkdir()
        run = self.init_run()
        self.hco("spawn", "--run", run, "--name", "w1", "--agent", "claude", "--brief", self.brief(),
                 "--cwd", str(sub), "--scope", "pkg/**")
        (sub / "mod.py").write_text("m")
        text = self.hco("collect", "--run", run).stdout
        self.assertIn("worker there): pkg/mod.py\n", text)  # the outbox in pkg/.hc-orchestrate is not listed
        self.assertIn("scope ok (pkg/**)", text)


class Units(unittest.TestCase):
    def test_demote_headings_skips_code_fences(self):
        md = "# Top\ntext\n```\n# not a heading\n```\n## Sub"
        self.assertEqual(hco.demote_headings(md, 3), "### Top\ntext\n```\n# not a heading\n```\n#### Sub")

    def test_scope_matching(self):
        cases = [
            ("src/auth/**", "src/auth/a.ts", True), ("src/auth/**", "src/auth/x/y.ts", True),
            ("src/auth/**", "src/authz/a.ts", False), ("src/auth/", "src/auth/x/y.ts", True),
            ("src/auth", "src/auth/a.ts", True), ("src/auth", "src/auth.ts", False),
            ("src/*.py", "src/a.py", True), ("src/*.py", "src/sub/a.py", False),
            ("*.md", "README.md", True), ("*.md", "docs/a.md", False),
            ("**/*.md", "a.md", True), ("**/*.md", "docs/x/a.md", True),
            ("src/**/test_*.py", "src/test_a.py", True), ("src/**/test_*.py", "src/a/b/test_a.py", True),
            ("src/**/test_*.py", "src/a/b/a.py", False), ("src/**/test_*.py", "test_a.py", False),
            ("a?c", "abc", True), ("a?c", "a/c", False),
            ("file[0-9].txt", "file3.txt", True), ("file[!0-9].txt", "filex.txt", True),
            ("file[!0-9].txt", "file3.txt", False),
            ("app/[id]/page.tsx", "app/[id]/page.tsx", True),  # literal brackets (Next.js routes)
            ("app/[id]", "app/[id]/page.tsx", True),
            ("docs/my file (1).md", "docs/my file (1).md", True), ("a+b.txt", "aab.txt", False),
            ("**", "any/where/at/all", True), ("src", "srcx/a", False),
        ]
        for pattern, path, want in cases:
            self.assertEqual(hco.in_scope(path, [pattern]), want, f"{pattern!r} vs {path!r}")

    def test_out_of_scope_without_patterns_checks_nothing(self):
        self.assertEqual(hco.out_of_scope(["anything"], None), [])
        self.assertEqual(hco.out_of_scope(["a/x", "b/y", "c"], ["a/**", "c"]), ["b/y"])


if __name__ == "__main__":
    unittest.main()
