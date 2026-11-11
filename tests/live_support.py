"""Shared harness for the live hco tests: a real herdr-core in an isolated session.

Each test class gets its own core: its own HC_SESSION and scratch XDG dirs under a short /tmp path
(unix socket paths are limited to ~103 bytes), so it never touches your real hc session. The class
stops only that core afterwards. Every test gets a fresh git repo and a "lead" terminal that runs
`sleep`; hco runs as that lead (HC_TERMINAL_ID set to its address), so workers become its children.

Skipped without `hc` on PATH or with HCDOC_NO_LIVE=1.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "scripts"
HCO, HCTEAM, HCDOC = SCRIPTS / "hco.py", SCRIPTS / "hcteam.py", SCRIPTS / "hcdoc.py"
BOT = HERE / "worker_bot.py"
HC = shutil.which("hc")
LIVE = bool(HC) and os.environ.get("HCDOC_NO_LIVE") != "1"
SKIP_WHY = "hc not installed or HCDOC_NO_LIVE=1"

# A template whose "agent" is tests/worker_bot.py. Default readiness: the bot reports OSC 7501 `idle` when it
# is ready, as Claude Code does, and core then types the task in.
BOT_TEMPLATE = f"""---
description: hco live tests: scripted stand-in for an agent worker
command: exec {sys.executable} -I {BOT}
---
"""


class LiveHc(unittest.TestCase):
    prefix = "hl"  # session name and temp dir prefix; subclasses pick their own
    counter = 0

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix=cls.prefix, dir="/tmp")
        cls.env = {k: v for k, v in os.environ.items()
                   if not k.startswith(("HC_", "HCDOC_", "HCBOARD_", "HCO_")) and k != "XDG_RUNTIME_DIR"}
        cls.env.update(HC_SESSION=f"{cls.prefix}{os.getpid() % 100000}", XDG_STATE_HOME=f"{cls.tmp}/s",
                       XDG_CONFIG_HOME=f"{cls.tmp}/c")
        tdir = Path(cls.tmp) / "c" / "herdr-core" / "templates"
        tdir.mkdir(parents=True)
        (tdir / "hcobot.md").write_text(BOT_TEMPLATE)
        for t in (HERE.parent / "templates").glob("*.md"):  # the skill's own templates, as install.sh does
            shutil.copy(t, tdir / t.name)
        r = cls.hc("ls", "--json")
        assert r.returncode == 0, r.stderr

    @classmethod
    def tearDownClass(cls):
        cls.hc("server", "stop")
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def hc(cls, *args, timeout=60):
        return subprocess.run([HC, *args], capture_output=True, text=True, env=cls.env, timeout=timeout)

    # ---------------------------------------------------------------- per test

    def setUp(self):
        LiveHc.counter += 1
        self.repo = Path(self.tmp) / f"r{LiveHc.counter}"
        self.repo.mkdir()
        for cmd in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
            subprocess.run(["git", *cmd], cwd=self.repo, check=True, capture_output=True)
        (self.repo / "README.md").write_text("live test repo\n")
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=self.repo, check=True, capture_output=True)
        r = self.hc("run", "--bg", "--json", "--name", f"lead{LiveHc.counter}", "--cwd", str(self.repo),
                    "--", "sleep", "3600")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.lead = json.loads(r.stdout)["address"]
        self.addCleanup(self.drop_terminals)

    def drop_terminals(self):
        """Kill and remove every terminal this test left (workers, checks, the lead)."""
        r = self.hc("ls", "--all", "--json")
        rows = json.loads(r.stdout).get("terminals", []) if r.returncode == 0 else []
        for t in rows:
            addr = t.get("address")
            if t.get("state") == "live":
                self.hc("kill", addr, "--json")
            self.hc("rm", addr, "--json")

    def hco(self, *args, check=True, timeout=300, env=None):
        p = subprocess.run([sys.executable, "-I", str(HCO), *args], cwd=self.repo, capture_output=True, text=True,
                           env={**self.env, "HC_TERMINAL_ID": self.lead, **(env or {})}, timeout=timeout)
        if check and p.returncode != 0:
            screen = ""
            try:  # a failed spawn: show what is on the worker's screen
                addr = json.loads(p.stdout).get("address")
                if addr:
                    time.sleep(1)
                    screen = f"\n--- screen of {addr} ---\n{self.screen(addr)}"
            except ValueError:
                pass
            self.fail(f"hco {args} exited {p.returncode}\n{p.stdout}\n{p.stderr}{screen}")
        return p

    def init(self, *extra):
        return json.loads(self.hco("init", "--task", "live test", *extra).stdout)["run"]

    def brief(self, name, text):
        f = Path(self.tmp) / f"{self.repo.name}-{name}.md"
        f.write_text(text)
        return str(f)

    def rundir(self, run):
        return self.repo / ".hc-orchestrate" / run

    def run_json(self, run):
        return json.loads((self.rundir(run) / "run.json").read_text())

    def status(self, run):
        return {r["worker"]: r for r in json.loads(self.hco("status", "--run", run, "--json").stdout)}

    def screen(self, address, lines=60):
        r = self.hc("read", address, "--lines", str(lines))
        return r.stdout if r.returncode == 0 else f"(hc read failed: {r.stderr.strip()})"

    def wait_reported(self, run, names, timeout):
        """Until every named worker's STATUS file has a line, or fail with each worker's screen.
        Uses `hco wait` (real child events from hc), which returns on news or after its own timeout."""
        deadline = time.time() + timeout
        while True:
            rows = self.status(run)
            pending = [n for n in names if rows.get(n, {}).get("outbox", "-") == "-"]
            if not pending:
                crashed = [n for n in names if "worker_bot crashed" in rows[n]["outbox"]]
                if crashed:
                    w = self.run_json(run)["workers"][crashed[0]]
                    self.fail(f"{crashed[0]}: {rows[crashed[0]]['outbox']}\n"
                              + Path(w["outbox"], "RESULT.md").read_text(errors="replace"))
                return rows
            if time.time() > deadline:
                screens = "\n".join(f"--- {n} ({rows.get(n, {}).get('state')}) ---\n{self.screen(rows[n]['address'])}"
                                    for n in pending if n in rows)
                self.fail(f"no STATUS from {pending} after {timeout} s\n{screens}")
            self.hco("wait", "--run", run, "--timeout", str(int(min(30, max(1, deadline - time.time())))),
                     timeout=120)
