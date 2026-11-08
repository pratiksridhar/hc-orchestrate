#!/usr/bin/env python3
"""hcboard: experiment board orchestrator on herdr-core's doc store (needs hcdoc.py, docstore_worker.py).

  init   EXP --spec SPEC.json [--parent P --delta JSON]   write job docs + manifest (state planned)
  run    EXP [--cwd D] [--poll S] [--no-commit]            the orchestrator loop; run it in an hc terminal
  status EXP [--json]                                      counts and a job table
  pause  EXP | resume EXP                                  flip the manifest between running and paused

SPEC.json: {"question": str, "variants": [str], "cases": [str], "job_cmd": [str, ...],
            "max_workers": 3, "max_attempts": 3, "pre": {...}, "out_dir": "runs/<exp>", "job_timeout_s": N}
job_cmd placeholders: {case} {variant} {out} {job} {exp} {attempt}. One job per case x variant.

`run` starts workers (labels exp=<id>, role=worker, hco.board=<id>), subscribes to doc and terminal
events before it reads anything, and reconciles (re-read every doc, compare owners with the live
worker terminals, requeue orphans, top up workers) on startup, on every manifest change, worker exit,
`missed`, reconnect, and every --poll seconds. Reconcile is idempotent. When every job is done or
failed it exports experiments/<exp>.json, commits it, sets the manifest to `archived`, deletes the
docs and removes its finished worker terminals. Run one orchestrator per experiment.

It prints one JSON event per line; the last line of a finished run is
`HCBOARD <exp> COMPLETE done=N failed=M`. Exit codes: 0 finished, 1 failed or gave up, 2 bad input, 4 core down.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import select
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import hcdoc  # noqa: E402
from docstore_worker import refuse_mainframe  # noqa: E402

HERE = Path(__file__).resolve().parent
HC = os.environ.get("HCBOARD_HC", "hc")
EXP_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,15}$")
FINAL = ("done", "failed")
MANIFEST_STATES = ("planned", "running", "paused", "done", "archived")
MIN_RECONCILE_GAP = 0.4  # seconds: coalesce bursts of events into one reconcile
MAX_BAD_EXITS = 10  # workers dying with no job finishing in between => give up


# ---------------------------------------------------------------- pure logic


def job_names(exp, n):
    return [f"{hcdoc.PREFIX}{exp}-j{i:03d}" for i in range(1, n + 1)]


def new_job(exp, case, variant, max_attempts):
    return {"schema": 1, "exp": exp, "variant": variant, "case": case, "state": "pending", "owner": None,
            "attempt": 0, "max_attempts": max_attempts, "started_ms": None, "finished_ms": None,
            "result": {"out": None, "summary": None, "error": None}}


def counts(jobs):
    c = {"pending": 0, "running": 0, "done": 0, "failed": 0, "missing": 0}
    for v in jobs.values():
        c["missing" if v is None else v.get("state") if v.get("state") in c else "missing"] += 1
    return c


def orphans(jobs, alive):
    """Jobs running under an owner that is not a live worker. Remote owners (machine/t7) are left alone."""
    return [(n, v) for n, v in jobs.items()
            if v and v.get("state") == "running" and "/" not in str(v.get("owner")) and v.get("owner") not in alive]


def exhausted(jobs):
    return [(n, v) for n, v in jobs.items()
            if v and v.get("state") == "pending" and v.get("attempt", 0) >= v.get("max_attempts", 1)]


def workers_to_start(max_workers, pending, running_alive, alive):
    """Workers needed in total = min(max_workers, jobs that still need one); start the difference."""
    return max(0, min(max_workers, pending + running_alive) - alive)


def requeue_ops(owner, attempt, max_attempts, why):
    final = attempt >= max_attempts
    return [("if", "state", "running"), ("if", "owner", owner), ("if", "attempt", attempt),
            ("set", "state", "failed" if final else "pending"), ("set", "owner", owner if final else None),
            ("set", "result.error", why), ("now", "finished_ms", None)]


def table(jobs):
    rows = [("job", "case", "variant", "state", "tries", "note")]
    for n, v in sorted(jobs.items()):
        if v is None:
            rows.append((n, "?", "?", "missing", "", ""))
            continue
        r = v.get("result") or {}
        rows.append((n, v.get("case", ""), v.get("variant", ""), v.get("state", ""),
                     f"{v.get('attempt', 0)}/{v.get('max_attempts', '?')}", (r.get("error") or r.get("summary") or "")[:60]))
    w = [max(len(str(r[i])) for r in rows) for i in range(6)]
    return "\n".join("  ".join(str(c).ljust(w[i]) for i, c in enumerate(r)).rstrip() for r in rows)


def by_variant(jobs):
    out = {}
    for v in jobs.values():
        if v:
            d = out.setdefault(v.get("variant", "?"), {"done": 0, "failed": 0, "other": 0})
            d[v["state"] if v.get("state") in FINAL else "other"] += 1
    return out


# ------------------------------------------------------------------ plumbing


def emit(event, **kw):
    sys.stdout.write(json.dumps({"event": event, "ts": hcdoc.now_ms(), **kw}, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def hc_json(args):
    try:
        p = subprocess.run([HC, *args], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise hcdoc.Down(f"hc {args[0]}: {e}")
    if p.returncode != 0:
        raise hcdoc.Down(f"hc {' '.join(args)} failed: {(p.stdout + p.stderr).strip()[:200]}")
    try:
        return json.loads(p.stdout)
    except ValueError:
        raise hcdoc.Down(f"hc {args[0]} printed non-JSON")


class Lines:
    """Non-blocking JSON-lines reader over a subprocess stdout."""

    def __init__(self, proc):
        self.proc, self.buf, self.eof = proc, b"", False

    def next(self, timeout):
        """A parsed message, None on timeout, False at EOF."""
        deadline = time.monotonic() + timeout
        while b"\n" not in self.buf:
            if self.eof:
                return False
            left = deadline - time.monotonic()
            r, _, _ = select.select([self.proc.stdout], [], [], max(0.0, left))
            if not r:
                return None
            chunk = os.read(self.proc.stdout.fileno(), 65536)
            if not chunk:
                self.eof = True
                return False
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        try:
            return json.loads(line)
        except ValueError:
            return None


def read_spec(path):
    try:
        with open(path, encoding="utf-8") as f:
            spec = json.load(f)
    except (OSError, ValueError) as e:
        raise hcdoc.BadInput(f"cannot read spec {path}: {e}")

    def strs(key):
        v = spec.get(key)
        if not (isinstance(v, list) and v and all(isinstance(x, str) and x and len(x) <= 200 for x in v)):
            raise hcdoc.BadInput(f"spec.{key} must be a non-empty list of short strings")
        return v

    cases, variants, cmd = strs("cases"), strs("variants"), strs("job_cmd")
    refuse_mainframe(cmd)
    for key, lo, hi, default in (("max_workers", 1, 16, 3), ("max_attempts", 1, 10, 3)):
        v = spec.setdefault(key, default)
        if type(v) is not int or not lo <= v <= hi:
            raise hcdoc.BadInput(f"spec.{key} must be an integer {lo}..{hi}")
    if not isinstance(spec.get("question"), str):
        raise hcdoc.BadInput("spec.question must be a string")
    if not isinstance(spec.setdefault("pre", {}), dict):
        raise hcdoc.BadInput("spec.pre must be an object")
    return spec, cases, variants, cmd


# ------------------------------------------------------------------ commands


def check_exp(exp):
    if not EXP_RE.match(exp):
        raise hcdoc.BadInput(f"bad experiment id {exp!r}: use a-z 0-9 _, start with a letter or digit, max 16")
    return exp


def cmd_init(a):
    exp = check_exp(a.exp)
    if a.delta and not a.parent:
        raise hcdoc.BadInput("--delta needs --parent")
    parent = check_exp(a.parent) if a.parent else None
    try:
        delta = json.loads(a.delta) if a.delta else None
    except ValueError as e:
        raise hcdoc.BadInput(f"--delta is not JSON: {e}")
    if delta is not None and not isinstance(delta, dict):
        raise hcdoc.BadInput("--delta must be a JSON object")
    spec, cases, variants, cmd = read_spec(a.spec)
    pairs = [(c, v) for c in cases for v in variants]
    if len(pairs) > 999:
        raise hcdoc.BadInput(f"{len(pairs)} jobs; the limit is 999")
    mname = hcdoc.check_name(hcdoc.PREFIX + exp)
    names = [hcdoc.check_name(n) for n in job_names(exp, len(pairs))]
    sess = hcdoc.Session()
    try:
        old, mver = sess.get(mname)
        if old is not None:
            raise hcdoc.BadInput(f"{mname} already exists (state {old.get('state')!r}); pick a new experiment id")
        versions = {}
        for n in names:
            v, ver = sess.get(n)
            if v is not None:
                raise hcdoc.BadInput(f"{n} already exists; is another experiment using this id?")
            versions[n] = ver
        for n, (case, variant) in zip(names, pairs):  # job docs first, so the manifest never names a missing doc
            sess.set(n, new_job(exp, case, variant, spec["max_attempts"]), versions[n])
        manifest = {"schema": 1, "exp": exp, "parent": parent, "delta": delta, "question": spec["question"],
                    "pre": spec["pre"], "cases": cases, "variants": variants, "jobs": names, "job_cmd": cmd,
                    "max_workers": spec["max_workers"], "state": "planned"}
        for key in ("out_dir", "job_timeout_s"):
            if key in spec:
                manifest[key] = spec[key]
        try:
            sess.set(mname, manifest, mver)
        except hcdoc.Conflict as e:
            raise hcdoc.Lost(f"{mname} was created by someone else meanwhile: {e}")
    finally:
        sess.close()
    print(json.dumps({"exp": exp, "manifest": mname, "jobs": len(names), "state": "planned"}))
    return hcdoc.OK


def load(sess, exp):
    mname = hcdoc.check_name(hcdoc.PREFIX + check_exp(exp))
    manifest, _ = sess.get(mname)
    if manifest is None:
        raise hcdoc.BadInput(f"{mname} does not exist")
    return mname, manifest, {n: sess.get(n)[0] for n in manifest.get("jobs") or []}


def cmd_status(a):
    sess = hcdoc.Session()
    try:
        mname, manifest, jobs = load(sess, a.exp)
    finally:
        sess.close()
    c = counts(jobs)
    if a.json:
        print(json.dumps({"manifest": manifest, "counts": c, "jobs": jobs}))
    else:
        print(f"{mname}  state={manifest.get('state')}  " + "  ".join(f"{k}={v}" for k, v in c.items() if v))
        print(table(jobs))
    return hcdoc.OK


def set_state(exp, frm, to):
    mname = hcdoc.check_name(hcdoc.PREFIX + check_exp(exp))
    sess = hcdoc.Session()
    try:
        _, new = sess.update(mname, lambda v: hcdoc.apply_ops(v, [("if", "state", frm), ("set", "state", to)]))
    except hcdoc.Lost as e:
        raise hcdoc.Lost(f"{mname}: expected state {frm!r}: {e}")
    finally:
        sess.close()
    print(json.dumps({"manifest": mname, "state": new["state"]}))
    return hcdoc.OK


# ---------------------------------------------------------------- orchestrator


class Orchestrator:
    def __init__(self, a):
        self.exp = check_exp(a.exp)
        self.mname = hcdoc.check_name(hcdoc.PREFIX + self.exp)
        self.cwd = os.path.abspath(a.cwd)
        self.poll = a.poll
        self.commit = not a.no_commit
        self.deadline = time.monotonic() + a.timeout if a.timeout else None
        self.sess = hcdoc.Session()
        self.watch = self.lines = None
        self.dirty = True
        self.last_state = None
        self.last_counts = None
        self.seen_bad = set()  # finished workers already counted as crashes
        self.bad_exits = 0
        self.children = set()
        self.worker_py = str(HERE / "docstore_worker.py")

    # -- events
    def start_watch(self):
        self.stop_watch()
        cmd = [sys.executable, "-I", str(HERE / "hcdoc.py"), "watch", "--exit-with-parent",
               "--label", f"hco.board={self.exp}"]
        self.watch = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.lines = Lines(self.watch)
        end = time.monotonic() + 30
        while time.monotonic() < end:  # the first line is the snapshot: the subscription is live
            m = self.lines.next(1.0)
            if m is False:
                break
            if m and m.get("kind") == "snapshot":
                return
        self.stop_watch()
        raise hcdoc.Down("could not subscribe to core events")

    def stop_watch(self):
        if self.watch:
            self.watch.kill()
            self.watch.wait()
            self.watch.stdout.close()
        self.watch = self.lines = None

    def on_message(self, m):
        kind = m.get("kind")
        if kind == "doc":
            n = m["name"]
            if n == self.mname or n.startswith(self.mname + "-j"):
                self.dirty = True
            elif re.match(r"^" + re.escape(hcdoc.PREFIX) + r"[a-z0-9_]+$", n) and n not in self.children:
                self.check_child(n)
        elif kind == "terminal" and m.get("type") in ("finished", "lost"):
            emit("worker_exit", terminal=m.get("terminal"), reason=m.get("type"))
            self.dirty = True
        elif kind == "snapshot" and m.get("reason") == "missed":
            emit("missed")
            self.dirty = True

    def check_child(self, name):
        try:
            v, _ = self.sess.get(name)
        except hcdoc.Fail:
            return
        if isinstance(v, dict) and v.get("parent") == self.exp:
            self.children.add(name)
            emit("child_experiment", manifest=name, exp=v.get("exp"), delta=v.get("delta"),
                 hint=f"start it with: hcboard.py run {v.get('exp')}")

    # -- workers
    def workers(self):
        return hc_json(["ls", "--all", "--json", "--label", f"hco.board={self.exp}"])["terminals"]

    def next_worker_name(self, terms):
        used = [int(m.group(1)) for t in terms if (m := re.match(rf"^{self.exp}-w(\d+)$", t.get("name") or ""))]
        return f"{self.exp}-w{max(used, default=0) + 1}"

    def start_worker(self, terms):
        name = self.next_worker_name(terms)
        r = subprocess.run(
            [HC, "run", "--bg", "--json", "--outlive-parent", "--no-notify", "--name", name,
             "--label", f"exp={self.exp}", "--label", "role=worker", "--label", f"hco.board={self.exp}",
             "--cwd", self.cwd, "--", sys.executable, self.worker_py, self.exp],
            capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            emit("worker_start_failed", name=name, error=(r.stdout + r.stderr).strip()[:200])
            return False
        emit("worker_started", name=name, terminal=json.loads(r.stdout).get("address"))
        terms.append({"name": name})
        return True

    # -- reconcile
    def reconcile(self):
        """Re-read everything and fix what is wrong. Safe to run any time. Returns True when all jobs are final."""
        terms = self.workers()  # before reading jobs: a worker we start later is never judged by an older list
        alive = {t["address"] for t in terms if t.get("state") == "live"}
        for t in terms:
            bad = t.get("state") == "finished" and t.get("exit_code") != 0
            if bad and t["address"] not in self.seen_bad:
                self.seen_bad.add(t["address"])
                self.bad_exits += 1
        _, manifest, jobs = load(self.sess, self.exp)
        state = manifest.get("state")
        if state != self.last_state:
            emit("manifest_state", state=state)
            self.last_state = state
        for n, v in orphans(jobs, alive):
            ops = requeue_ops(v["owner"], v["attempt"], v.get("max_attempts", 1), f"worker {v['owner']} died")
            try:
                _, jobs[n] = self.sess.update(n, lambda d, ops=ops: hcdoc.apply_ops(d, ops))
                emit("requeued", job=n, owner=v["owner"], attempt=v["attempt"], to=jobs[n]["state"])
            except hcdoc.Lost:
                jobs[n] = self.sess.get(n)[0]  # it moved on while we looked
        for n, v in exhausted(jobs):
            ops = [("if", "state", "pending"), ("set", "state", "failed"),
                   ("set", "result.error", "attempts exhausted"), ("now", "finished_ms", None)]
            try:
                _, jobs[n] = self.sess.update(n, lambda d, ops=ops: hcdoc.apply_ops(d, ops))
            except hcdoc.Lost:
                jobs[n] = self.sess.get(n)[0]
        c = counts(jobs)
        if c != self.last_counts:
            if self.last_counts and c["done"] + c["failed"] > self.last_counts["done"] + self.last_counts["failed"]:
                self.bad_exits = 0  # progress: earlier crashes were not systematic
            emit("progress", **c)
            self.last_counts = c
        if c["missing"]:
            emit("warning", message=f"{c['missing']} job doc(s) missing", jobs=[n for n, v in jobs.items() if v is None])
        if self.bad_exits >= MAX_BAD_EXITS:
            raise hcdoc.Fail(f"{self.bad_exits} workers died with no job finishing; giving up (see `hc read` on a worker)")
        if c["pending"] + c["running"] == 0:
            return True
        if state == "running":
            running_alive = sum(1 for v in jobs.values() if v and v.get("state") == "running" and v.get("owner") in alive)
            for _ in range(workers_to_start(manifest.get("max_workers", 1), c["pending"], running_alive, len(alive))):
                if not self.start_worker(terms):
                    break
        return False

    # -- finish
    def finalize(self):
        mname, manifest, jobs = load(self.sess, self.exp)
        if manifest.get("state") not in ("done", "archived"):
            self.sess.update(mname, lambda v: hcdoc.apply_ops(v, [("set", "state", "done")]))
        out = os.path.join(self.cwd, "experiments", f"{self.exp}.json")
        res = self.sess.export(self.exp, out)
        if res["missing"] or res["jobs"] != len(manifest.get("jobs") or []):
            raise hcdoc.Fail(f"export is incomplete ({res}); docs kept")
        emit("exported", **res)
        if self.commit:
            self.git_commit(out)
        self.sess.update(mname, lambda v: hcdoc.apply_ops(v, [("set", "state", "archived")]))
        self.delete_docs(mname, manifest)
        removed = self.remove_finished_workers()
        c = counts(jobs)
        print()
        print(table(jobs))
        print()
        for variant, d in sorted(by_variant(jobs).items()):
            print(f"{variant:<24} done={d['done']} failed={d['failed']}")
        emit("archived", removed_workers=removed, out=out)
        print(f"HCBOARD {self.exp} COMPLETE done={c['done']} failed={c['failed']}", flush=True)
        return hcdoc.OK

    def git_commit(self, path):
        rel = os.path.relpath(path, self.cwd)
        for args in (["add", "--", rel], ["commit", "-m", f"board: export experiment {self.exp}", "--", rel]):
            r = subprocess.run(["git", "-C", self.cwd, *args], capture_output=True, text=True)
            if r.returncode != 0:
                raise hcdoc.Fail(f"git {args[0]} failed ({(r.stdout + r.stderr).strip()[:200]}); docs kept, "
                                 f"fix and rerun `hcboard.py run {self.exp}` (or --no-commit)")
        emit("committed", path=rel)

    def delete_docs(self, mname, manifest):
        for n in list(manifest.get("jobs") or []) + [mname]:
            v, ver = self.sess.get(n)
            if v is not None:
                try:
                    self.sess.set(n, None, ver)
                except hcdoc.Conflict:
                    emit("warning", message=f"{n} changed during cleanup; left in place")

    def remove_finished_workers(self):
        n = 0
        for t in self.workers():
            if t.get("state") == "finished":  # only our own labeled terminals come back from this query
                subprocess.run([HC, "rm", t["address"]], capture_output=True, timeout=60)
                n += 1
        return n

    # -- main loop
    def run(self):
        mname, manifest, _ = load(self.sess, self.exp)
        if manifest.get("state") == "archived":
            return self.resume_archived(mname, manifest)
        if manifest.get("state") == "planned":
            self.sess.update(mname, lambda v: hcdoc.apply_ops(v, [("if", "state", "planned"), ("set", "state", "running")]))
        self.start_watch()  # subscribe first, then read
        emit("started", manifest=mname, cwd=self.cwd)
        last, due = 0.0, time.monotonic() + self.poll
        try:
            while True:
                m = self.lines.next(0.5) if self.lines else False
                if m is False:  # watcher died (core restart, dropped connection): resubscribe, then reconcile
                    emit("watch_restarted")
                    time.sleep(1)
                    self.start_watch()
                    self.dirty = True
                elif m:
                    self.on_message(m)
                now = time.monotonic()
                if (self.dirty and now - last >= MIN_RECONCILE_GAP) or now >= due:
                    self.dirty = False
                    last, due = now, now + self.poll
                    if self.reconcile():
                        break
                if self.deadline and now > self.deadline:
                    raise hcdoc.Fail("--timeout reached; experiment left in place, rerun to continue")
        finally:
            self.stop_watch()
        return self.finalize()

    def resume_archived(self, mname, manifest):
        out = os.path.join(self.cwd, "experiments", f"{self.exp}.json")
        if not os.path.exists(out):
            raise hcdoc.Fail(f"{mname} is archived but {out} is missing; not deleting anything")
        self.delete_docs(mname, manifest)
        emit("archived", resumed=True, out=out)
        return hcdoc.OK

    def close(self):
        self.stop_watch()
        self.sess.close()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="hcboard", description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init")
    p.add_argument("exp")
    p.add_argument("--spec", required=True)
    p.add_argument("--parent")
    p.add_argument("--delta", help="JSON object describing what changed from the parent")
    p = sub.add_parser("run")
    p.add_argument("exp")
    p.add_argument("--cwd", default=os.getcwd(), help="where workers run and experiments/<exp>.json is written (a git repo)")
    p.add_argument("--poll", type=float, default=30, help="reconcile at least this often, seconds (default 30)")
    p.add_argument("--timeout", type=float, help="give up after this many seconds (default: no limit)")
    p.add_argument("--no-commit", action="store_true", help="export but do not git commit (then docs are still deleted)")
    p = sub.add_parser("status")
    p.add_argument("exp")
    p.add_argument("--json", action="store_true")
    for name in ("pause", "resume"):
        sub.add_parser(name).add_argument("exp")
    a = ap.parse_args(argv)
    orch = None
    try:
        if a.cmd == "init":
            return cmd_init(a)
        if a.cmd == "status":
            return cmd_status(a)
        if a.cmd == "pause":
            return set_state(a.exp, "running", "paused")
        if a.cmd == "resume":
            return set_state(a.exp, "paused", "running")
        orch = Orchestrator(a)
        return orch.run()
    except hcdoc.Conflict as e:
        err = hcdoc.Lost(str(e))
    except hcdoc.Fail as e:
        err = e
    except KeyboardInterrupt:
        return 1
    finally:
        if orch:
            orch.close()
    sys.stderr.write(json.dumps({"error": str(err), "code": err.code}) + "\n")
    return 1 if err.code == hcdoc.LOST else err.code


if __name__ == "__main__":
    sys.exit(main())
