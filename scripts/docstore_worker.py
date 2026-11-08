#!/usr/bin/env python3
"""docstore_worker: claim and run jobs from an experiment board (see hcboard.py).

    hc run --bg --name w1 --label exp=e051 --label role=worker -- python3 docstore_worker.py e051

Loop: read the manifest (stop unless its state is `running`), walk its jobs, claim the first `pending`
one with hcdoc's CAS claim (owner = $HC_TERMINAL_ID), run the manifest's `job_cmd` with {case} {variant}
{out} {job} {exp} {attempt} filled in, write the result, repeat. Exits 0 when no job can be claimed.

Every result write is guarded by `owner == me and attempt == mine`, so a worker the orchestrator
already gave up on cannot overwrite a newer attempt. A failed job goes back to `pending` while
attempt < max_attempts (this worker may retry it at once), else `failed`. If the worker itself is
killed, the job stays `running` with a dead owner and the orchestrator requeues it.

Delivery is at-least-once: a worker killed after job_cmd finished but before the result write means
the job runs again, so job_cmd should be idempotent. Output goes to files (runs/<exp>/<job>.json and
.log, under the worker's cwd); docs only keep the path and a short summary.

Exit codes: 0 done, 2 bad input or not in hc, 4 core unavailable.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import hcdoc  # noqa: E402

PLACEHOLDER = re.compile(r"\{(case|variant|out|job|exp|attempt)\}")
SAFE_DIR = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]*$")
MAINFRAME = re.compile(r"\bzowe\b", re.I)  # work policy: no agent-issued mainframe commands
SUMMARY_MAX = 300
LOG_TAIL = 300

CHILD = None  # the running job_cmd, so a kill of this worker also stops it
OWNER = "?"


def log(msg):
    print(f"[worker {OWNER}] {msg}", flush=True)


def refuse_mainframe(argv):
    for part in argv:
        if MAINFRAME.search(part):
            raise hcdoc.BadInput(f"job_cmd mentions zowe ({part!r}); mainframe commands are not allowed here")


def fill(argv, values):
    return [PLACEHOLDER.sub(lambda m: str(values[m.group(1)]), a) for a in argv]


def out_dir_for(manifest):
    d = manifest.get("out_dir") or f"runs/{manifest['exp']}"
    if not isinstance(d, str) or not SAFE_DIR.match(d) or ".." in d.split("/"):
        raise hcdoc.BadInput(f"unsafe out_dir {d!r}: use a relative path without ..")
    return d


def tail(path, n=LOG_TAIL):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - n))
            return " ".join(f.read().decode("utf-8", "replace").split())
    except OSError:
        return ""


def summarize(out_path):
    """Short summary from the job's output file: its `summary` key, else the compact JSON, else None."""
    try:
        if os.path.getsize(out_path) > 4096:
            return None
        with open(out_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    text = data["summary"] if isinstance(data, dict) and isinstance(data.get("summary"), str) \
        else json.dumps(data, separators=(",", ":"))
    return text[:SUMMARY_MAX]


def on_signal(signum, _frame):
    if CHILD is not None and CHILD.poll() is None:
        try:
            os.killpg(CHILD.pid, signal.SIGKILL)
        except OSError:
            pass
    os._exit(128 + signum)  # job stays `running` with a dead owner: the orchestrator requeues it


def run_command(argv, cwd, log_path, timeout):
    """Run job_cmd in its own process group. Returns (exit code or None, error text or None)."""
    global CHILD
    with open(log_path, "wb") as logf:
        try:
            CHILD = subprocess.Popen(argv, cwd=cwd, stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as e:
            return 127, f"cannot run {argv[0]}: {e}"
        try:
            rc = CHILD.wait(timeout=timeout)
            return rc, None
        except subprocess.TimeoutExpired:
            os.killpg(CHILD.pid, signal.SIGKILL)
            CHILD.wait()
            return None, f"timed out after {timeout}s"
        finally:
            CHILD = None


def claim_next(sess, manifest, owner):
    for jname in manifest.get("jobs") or []:
        v, _ = sess.get(jname)
        if not isinstance(v, dict) or v.get("state") != "pending":
            continue
        try:
            _, claimed = sess.update(jname, hcdoc.claim_mutation(owner))
            return jname, claimed
        except hcdoc.Lost:
            continue  # someone else won it, or its attempts ran out
    return None, None


def finish_ops(owner, attempt, rc, err, out_path, max_attempts):
    guard = [("if", "owner", owner), ("if", "attempt", attempt), ("if", "state", "running")]
    if rc == 0:
        return guard + [("set", "state", "done"), ("set", "result.out", out_path if os.path.exists(out_path) else None),
                        ("set", "result.summary", summarize(out_path)), ("set", "result.error", None),
                        ("now", "finished_ms", None)]
    retry = attempt < max_attempts
    return guard + [("set", "state", "pending" if retry else "failed"), ("set", "owner", None if retry else owner),
                    ("set", "result.out", None), ("set", "result.summary", None), ("set", "result.error", err),
                    ("now", "finished_ms", None)]


def run_job(sess, manifest, jname, job, owner):
    exp, attempt = manifest["exp"], job["attempt"]
    out_dir = out_dir_for(manifest)
    os.makedirs(out_dir, exist_ok=True)
    out_path, log_path = f"{out_dir}/{jname}.json", f"{out_dir}/{jname}.log"
    if os.path.exists(out_path):
        os.remove(out_path)  # nothing from an earlier attempt may pass for this one's result
    cmd = manifest.get("job_cmd")
    if not (isinstance(cmd, list) and cmd and all(isinstance(a, str) for a in cmd)):
        rc, err = 2, "manifest job_cmd must be a non-empty list of strings"
    else:
        values = {"case": job.get("case", ""), "variant": job.get("variant", ""), "out": out_path,
                  "job": jname, "exp": exp, "attempt": attempt}
        try:
            refuse_mainframe(cmd)
            argv = fill(cmd, values)
            log(f"running {jname} attempt {attempt}: {' '.join(argv)}")
            rc, err = run_command(argv, os.getcwd(), log_path, manifest.get("job_timeout_s"))
        except hcdoc.BadInput as e:
            rc, err = 2, str(e)
    if rc != 0 and err is None:
        err = f"exit {rc}: {tail(log_path)}" if os.path.exists(log_path) else f"exit {rc}"
    ops = finish_ops(owner, attempt, rc, err, out_path, job.get("max_attempts", 1))
    try:
        sess.update(jname, lambda v: hcdoc.apply_ops(v, ops))
    except hcdoc.Lost as e:
        log(f"{jname}: result not written, no longer mine ({e})")
        return
    log(f"{jname} -> {'done' if rc == 0 else 'failed attempt ' + str(attempt)}")


def main(argv=None):
    global OWNER
    ap = argparse.ArgumentParser(prog="docstore_worker", description=__doc__,
                                 formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("exp", help="experiment id, e.g. e051")
    ap.add_argument("--owner", default=os.environ.get("HC_TERMINAL_ID"),
                    help="address recorded as job owner (default $HC_TERMINAL_ID)")
    a = ap.parse_args(argv)
    if not a.owner:
        sys.stderr.write("docstore_worker: no $HC_TERMINAL_ID; start me with `hc run`\n")
        return hcdoc.BAD
    OWNER = a.owner
    for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, on_signal)
    sess = hcdoc.Session()
    try:
        mname = hcdoc.check_name(hcdoc.PREFIX + a.exp)
        while True:
            manifest, _ = sess.get(mname)
            if manifest is None:
                log(f"{mname} does not exist; nothing to do")
                return hcdoc.OK
            if manifest.get("state") != "running":
                log(f"manifest is {manifest.get('state')!r}; not claiming")
                return hcdoc.OK
            jname, job = claim_next(sess, manifest, a.owner)
            if jname is None:
                log("no pending jobs; exiting")
                return hcdoc.OK
            run_job(sess, manifest, jname, job, a.owner)
    except hcdoc.Fail as e:
        sys.stderr.write(json.dumps({"error": str(e), "code": e.code}) + "\n")
        return e.code
    finally:
        sess.close()


if __name__ == "__main__":
    sys.exit(main())
