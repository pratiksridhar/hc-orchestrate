#!/usr/bin/env python3
"""A scripted stand-in for an agent worker, for the live tests (tests/test_hco_live.py).

hc starts it through the `hcobot` template (tests/live_support.py), so it runs in a real terminal with
a real $HC_TERMINAL_ID. It ignores what is typed into the terminal. It finds its run and name from its
own labels (`hc get $HC_TERMINAL_ID`), reads its BRIEF.md and acts on one line in it:

    BOT {"write": {"path": "text"}, "claim": true, "mail": [["to", "text"]], "wait_mail": 1, "status": "DONE"}

  write      files to create, relative to the worker's folder (before anything else)
  claim      work through the team's task list: claim, write the task title to task-<n>.txt, done
  mail       messages to send with hcteam (after the tasks)
  wait_mail  wait (up to 60 s) until this many messages are in the inbox, before the tasks
  status     the STATUS line to report (default DONE)

Then it writes RESULT.md (what it did, its inbox, every command's output), STATUS, and reports to its
lead with `hc send`, exactly as a brief tells an agent to, and stays open like an idle agent. Like
Claude Code and Codex it reports OSC 7501 status: idle when ready for the task, working while it works,
idle when the turn is over. If it
crashes, STATUS says `FAILED: worker_bot crashed: <error>` and RESULT.md holds the traceback.
"""
import json
import os
import re
import select
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
HCTEAM = HERE.parent / "scripts" / "hcteam.py"
log = []


def sh(*argv):
    p = subprocess.run(list(argv), capture_output=True, text=True, timeout=120)
    log.append(f"$ {' '.join(map(str, argv))}\nexit {p.returncode}\n{p.stdout}{p.stderr}")
    return p


def team(run, me, verb, *args):
    return sh(sys.executable, "-I", str(HCTEAM), verb, "--run", run, "--as", me, *args)


def status(state):
    """Report an OSC 7501 program status, as Claude Code and Codex do. hc's template launch waits for the
    first report before it types the task in; `hco wait` gets turn events from the later ones."""
    sys.stdout.write(f"\x1b]7501;state={state}:app=hcobot\x1b\\")
    sys.stdout.flush()


def wait_for_input(timeout):
    """True once a line was typed into the terminal (the task, or a follow-up), False after timeout."""
    r, _, _ = select.select([sys.stdin], [], [], timeout)
    if r:
        sys.stdin.readline()
        return True
    return False


def main():
    me_addr = os.environ["HC_TERMINAL_ID"]
    rec = json.loads(sh("hc", "get", me_addr, "--json").stdout)
    labels = rec.get("labels") or {}
    run, name, cwd = labels["hco.run"], labels["hco.worker"], Path(rec.get("cwd") or os.getcwd())
    outbox = cwd / ".hc-orchestrate" / run / "out" / name
    brief = (outbox / "BRIEF.md").read_text()
    m = re.search(r"^BOT (\{.*\})\s*$", brief, re.M)
    plan = json.loads(m.group(1)) if m else {}
    lead = re.search(r"hc send (\S+) \"\[hco ", brief).group(1)
    did, inbox = [], []

    for path, text in (plan.get("write") or {}).items():
        (cwd / path).write_text(text)
        did.append(f"wrote {path}")

    want = plan.get("wait_mail", 0)
    deadline = time.time() + 60
    while len(inbox) < want and time.time() < deadline:
        out = team(run, name, "inbox").stdout
        inbox += [line for line in out.splitlines() if line.startswith("#")]
        if len(inbox) < want:
            time.sleep(1)

    if plan.get("claim"):
        while True:
            p = team(run, name, "claim")
            m = re.search(r"task #(\d+) is yours: (.*)", p.stdout)
            if not m:
                did.append("claim: " + (p.stderr.strip() or p.stdout.strip()).splitlines()[0])
                break
            n, title = m.group(1), m.group(2)
            (cwd / f"task-{n}.txt").write_text(title + "\n")
            team(run, name, "done", n, "--note", f"wrote task-{n}.txt")
            did.append(f"task #{n}: {title}")

    for to, text in plan.get("mail") or []:
        p = team(run, name, "send", "--to", to, text)
        did.append(f"mail to {to}: exit {p.returncode}")

    out = team(run, name, "inbox").stdout
    inbox += [line for line in out.splitlines() if line.startswith("#")]
    status = plan.get("status", "DONE")
    (outbox / "RESULT.md").write_text("# worker_bot\n\n" + "\n".join(f"- {d}" for d in did)
                                      + "\n\n## inbox\n\n" + "\n".join(inbox or ["(none)"])
                                      + "\n\n## log\n\n```\n" + "\n".join(log) + "\n```\n")
    (outbox / "STATUS").write_text(status + "\n")
    sh("hc", "send", lead, f"[hco {run}/{name}] {status} - {outbox}/RESULT.md")


def stay():
    """Stay open after reporting, as an agent does: hc counts a program that exits before it is ready as
    a failed launch (first_message.sent false), and the lead may still send follow-ups. Each one is a turn
    that ends at once. Killed by cleanup."""
    status("idle")
    while True:
        if wait_for_input(3600):
            status("working")
            status("idle")


if __name__ == "__main__":
    status("idle")  # ready for the task
    wait_for_input(20)  # the task as typed in; the work itself comes from BRIEF.md, so go on without it
    status("working")
    try:
        main()
    except Exception:  # report the crash where the lead and the tests look, then stay open
        import traceback
        err = traceback.format_exc()
        print(err, flush=True)
        try:
            rec = json.loads(subprocess.run(["hc", "get", os.environ.get("HC_TERMINAL_ID", ""), "--json"],
                                            capture_output=True, text=True, timeout=60).stdout)
            labels = rec.get("labels") or {}
            out = Path(rec.get("cwd") or os.getcwd()) / ".hc-orchestrate" / labels["hco.run"] / "out" / labels["hco.worker"]
            (out / "RESULT.md").write_text("# worker_bot crashed\n\n```\n" + err + "\n" + "\n".join(log) + "\n```\n")
            (out / "STATUS").write_text("FAILED: worker_bot crashed: " + err.strip().splitlines()[-1] + "\n")
        except Exception as e:
            print(f"could not write the outbox either: {e}", flush=True)
    stay()
