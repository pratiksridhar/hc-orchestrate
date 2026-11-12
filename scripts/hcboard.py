#!/usr/bin/env python3
"""hcboard: experiment board orchestrator on herdr-core's doc store (needs hcdoc.py, docstore_worker.py).

  init   EXP --spec SPEC.json [--parent P --delta JSON]   write job docs + manifest (state planned)
  init   EXP --retry-failed P [--spec SPEC.json]           a new experiment with only P's failed jobs, read
                                                           from experiments/P.json (P stays as it is)
  run    EXP [--cwd D] [--poll S] [--no-commit]            the orchestrator loop; run it in an hc terminal
  status EXP [--json]                                      counts, timing, per-variant totals and a job table
  pause  EXP | resume EXP                                  flip the manifest between running and paused
  cancel EXP                                               stop it: the orchestrator kills its workers, fails
                                                           the unfinished jobs as "cancelled" and archives

SPEC.json: {"question": str, "variants": [str], "cases": [str], "job_cmd": [str, ...],
            "max_workers": 3, "max_attempts": 3, "pre": {...}, "out_dir": "runs/<exp>", "job_timeout_s": N,
            "retry_delay_s": N, "pairs": [[case, variant], ...]}
job_cmd placeholders: {case} {variant} {out} {job} {exp} {attempt}. One job per case x variant, or, with
"pairs", one per listed pair. retry_delay_s: a failed job waits before its retry (doubling, max 10 min).

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
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import hcdoc  # noqa: E402
from docstore_worker import PLACEHOLDER, out_dir_for, refuse_mainframe  # noqa: E402

HERE = Path(__file__).resolve().parent
HC = os.environ.get("HCBOARD_HC", "hc")
EXP_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,15}$")
FINAL = ("done", "failed")
MANIFEST_STATES = ("planned", "running", "paused", "cancelled", "done", "archived")
MIN_RECONCILE_GAP = 0.4  # seconds: coalesce bursts of events into one reconcile
MAX_BAD_EXITS = 10  # workers dying with no job finishing in between => give up
WATCH_RETRY_S = 60  # how long a dropped watcher keeps trying to resubscribe


class GaveUp(hcdoc.Fail):
    """The run stopped without finishing (exit 1); start-up checks stay BadInput (exit 2)."""
    code = 1


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


def fmt_secs(s):
    s = int(s)
    return f"{s // 3600}h{s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s}s"


def stats(jobs, max_workers, now_ms):
    """Timing from the job docs: elapsed since the first start, mean seconds per finished job, and a rough
    time left (unfinished jobs x mean / workers). None where there is nothing to go on yet."""
    live = [v for v in jobs.values() if v]
    starts = [v["started_ms"] for v in live if type(v.get("started_ms")) is int]
    took = [(v["finished_ms"] - v["started_ms"]) / 1000 for v in live
            if v.get("state") in FINAL and type(v.get("started_ms")) is int and type(v.get("finished_ms")) is int
            and v["finished_ms"] >= v["started_ms"]]
    left = sum(1 for v in live if v.get("state") not in FINAL)
    mean = sum(took) / len(took) if took else None
    return {"elapsed_s": round((now_ms - min(starts)) / 1000, 1) if starts else None,
            "mean_job_s": round(mean, 1) if mean is not None else None,
            "eta_s": round(left * mean / max(1, min(max_workers, left)), 1) if mean is not None and left else
            (0 if not left else None), "retries": sum(max(0, v.get("attempt", 0) - 1) for v in live)}


def cancel_mutation(v):
    """Fail a job that has not finished; a job that finished meanwhile is left as it is."""
    if v.get("state") in FINAL:
        raise hcdoc.Lost("already final")
    v["state"] = "failed"
    v.setdefault("result", {})["error"] = "cancelled"
    v["finished_ms"] = hcdoc.now_ms()
    return v


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
    if not isinstance(spec, dict):
        raise hcdoc.BadInput(f"spec {path} must be a JSON object")
    return check_spec(spec)


def check_spec(spec):
    """Validate a spec and fill defaults. Returns (spec, cases, variants, job_cmd, pairs)."""
    def strs(key, v):
        if not (isinstance(v, list) and v and all(isinstance(x, str) and x and len(x) <= 200 for x in v)):
            raise hcdoc.BadInput(f"spec.{key} must be a non-empty list of short strings")
        return v

    pairs = spec.get("pairs")
    if pairs is not None:
        if not (isinstance(pairs, list) and pairs and all(
                isinstance(x, list) and len(x) == 2 and all(isinstance(y, str) and y and len(y) <= 200 for y in x)
                for x in pairs)):
            raise hcdoc.BadInput('spec.pairs must be a non-empty list of ["case", "variant"] pairs')
        pairs = [tuple(x) for x in pairs]
        if len(set(pairs)) != len(pairs):
            raise hcdoc.BadInput("spec.pairs lists a pair twice")
        spec.setdefault("cases", list(dict.fromkeys(c for c, _ in pairs)))
        spec.setdefault("variants", list(dict.fromkeys(v for _, v in pairs)))
    cases, variants, cmd = strs("cases", spec.get("cases")), strs("variants", spec.get("variants")), \
        strs("job_cmd", spec.get("job_cmd"))
    if pairs is None:
        pairs = [(c, v) for c in cases for v in variants]
    elif any(c not in cases or v not in variants for c, v in pairs):
        raise hcdoc.BadInput("spec.pairs names a case or variant that spec.cases / spec.variants does not list")
    refuse_mainframe(cmd)
    for key, lo, hi, default in (("max_workers", 1, 16, 3), ("max_attempts", 1, 10, 3)):
        v = spec.setdefault(key, default)
        if type(v) is not int or not lo <= v <= hi:
            raise hcdoc.BadInput(f"spec.{key} must be an integer {lo}..{hi}")
    for key in ("job_timeout_s", "retry_delay_s"):
        v = spec.get(key)
        if v is not None and (type(v) not in (int, float) or v < 0 or (key == "job_timeout_s" and v == 0)):
            raise hcdoc.BadInput(f"spec.{key} must be a number of seconds" + (" above 0" if key == "job_timeout_s" else ""))
    if not isinstance(spec.get("question"), str):
        raise hcdoc.BadInput("spec.question must be a string")
    if not isinstance(spec.setdefault("pre", {}), dict):
        raise hcdoc.BadInput("spec.pre must be an object")
    if spec.get("out_dir") is not None:
        out_dir_for({**spec, "exp": "x"})
    return spec, cases, variants, cmd, pairs


def spec_from_failed(parent, export_path, override):
    """A spec for rerunning the failed jobs of an exported experiment: the parent's manifest settings
    (or `override`, a spec the caller read) with `pairs` set to the failed (case, variant) pairs."""
    try:
        with open(export_path, encoding="utf-8") as f:
            dump = json.load(f)
        manifest, jobs = dump["manifest"], dump["jobs"]
        if dump.get("exp") != parent or not isinstance(manifest, dict) or not isinstance(jobs, dict):
            raise ValueError(f"not an export of {parent}")
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise hcdoc.BadInput(f"cannot use {export_path} as the export of {parent}: {e}. An experiment is "
                             "exported when it finishes; pass --export PATH if the file is elsewhere")
    failed = [[v.get("case"), v.get("variant")] for _, v in sorted(jobs.items())
              if isinstance(v, dict) and v.get("state") == "failed"]
    if not failed:
        raise hcdoc.BadInput(f"{parent} has no failed jobs; nothing to retry")
    spec = dict(override) if override is not None else {
        k: manifest[k] for k in ("question", "pre", "job_cmd", "max_workers", "job_timeout_s", "retry_delay_s")
        if k in manifest}
    if override is None:
        tries = [v.get("max_attempts") for v in jobs.values() if isinstance(v, dict)]
        if tries and type(tries[0]) is int:
            spec["max_attempts"] = tries[0]
    spec.pop("cases", None)
    spec.pop("variants", None)
    spec["pairs"] = failed
    return spec


# ------------------------------------------------------------------ commands


def check_exp(exp):
    if not EXP_RE.match(exp):
        raise hcdoc.BadInput(f"bad experiment id {exp!r}: use a-z 0-9 _, start with a letter or digit, max 16")
    return exp


def cmd_init(a):
    exp = check_exp(a.exp)
    if a.retry_failed and a.parent and a.parent != a.retry_failed:
        raise hcdoc.BadInput("--retry-failed already names the parent; drop --parent")
    if not a.retry_failed and not a.spec:
        raise hcdoc.BadInput("init needs --spec SPEC.json (or --retry-failed PARENT)")
    a.parent = a.parent or a.retry_failed
    if a.delta and not a.parent:
        raise hcdoc.BadInput("--delta needs --parent")
    parent = check_exp(a.parent) if a.parent else None
    try:
        delta = json.loads(a.delta) if a.delta else None
    except ValueError as e:
        raise hcdoc.BadInput(f"--delta is not JSON: {e}")
    if delta is not None and not isinstance(delta, dict):
        raise hcdoc.BadInput("--delta must be a JSON object")
    if a.retry_failed:
        if parent == exp:
            raise hcdoc.BadInput("a retry is a new experiment: give it its own id")
        export = a.export or os.path.join(a.cwd, "experiments", f"{parent}.json")
        override = read_spec(a.spec)[0] if a.spec else None
        spec, cases, variants, cmd, pairs = check_spec(spec_from_failed(parent, export, override))
        if delta is None:
            delta = {"retry_failed": len(pairs)}
    else:
        spec, cases, variants, cmd, pairs = read_spec(a.spec)
    if len(pairs) > 999:
        raise hcdoc.BadInput(f"{len(pairs)} jobs; the limit is 999")
    mname = hcdoc.check_name(hcdoc.PREFIX + exp)
    names = [hcdoc.check_name(n) for n in job_names(exp, len(pairs))]
    sess = hcdoc.Session()
    try:
        old, mver = sess.get(mname)
        if old is not None:
            raise hcdoc.BadInput(f"{mname} already exists (state {old.get('state')!r}); pick a new experiment id")
        if mver != 0:  # deleted docs are null at a version > 0: an archived experiment had this id
            raise hcdoc.BadInput(f"{mname} was used by an experiment that is archived; pick a new experiment id")
        manifest = {"schema": 1, "exp": exp, "parent": parent, "delta": delta, "question": spec["question"],
                    "pre": spec["pre"], "cases": cases, "variants": variants, "jobs": names, "job_cmd": cmd,
                    "max_workers": spec["max_workers"], "state": "planned"}
        for key in ("out_dir", "job_timeout_s", "retry_delay_s"):
            if spec.get(key) is not None:
                manifest[key] = spec[key]
        hcdoc.check_value(manifest)  # before any job doc: a refused manifest must not leave them behind
        versions = {}
        for n in names:
            v, ver = sess.get(n)
            if v is not None:
                raise hcdoc.BadInput(f"{n} already exists; is another experiment using this id?")
            versions[n] = ver
        for n, (case, variant) in zip(names, pairs):  # job docs first, so the manifest never names a missing doc
            sess.set(n, new_job(exp, case, variant, spec["max_attempts"]), versions[n])
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
    st = stats(jobs, manifest.get("max_workers", 1), hcdoc.now_ms())
    if a.json:
        print(json.dumps({"manifest": manifest, "counts": c, "stats": st, "variants": by_variant(jobs), "jobs": jobs}))
    else:
        print(f"{mname}  state={manifest.get('state')}  " + "  ".join(f"{k}={v}" for k, v in c.items() if v))
        if st["elapsed_s"] is not None:
            print(f"running for {fmt_secs(st['elapsed_s'])}"
                  + (f", {st['mean_job_s']:g} s per finished job" if st["mean_job_s"] is not None else "")
                  + (f", about {fmt_secs(st['eta_s'])} left at {manifest.get('max_workers', 1)} worker(s) (rough)"
                     if st["eta_s"] else "") + (f", {st['retries']} retried attempt(s)" if st["retries"] else ""))
        for variant, d in sorted(by_variant(jobs).items()):
            print(f"  {variant:<24} done={d['done']} failed={d['failed']} open={d['other']}")
        print(table(jobs))
    return hcdoc.OK


def cmd_cancel(a):
    """Mark the experiment cancelled. The orchestrator does the rest on its next reconcile; if none is
    running, `hcboard.py run EXP` does it."""
    mname = hcdoc.check_name(hcdoc.PREFIX + check_exp(a.exp))

    def mutate(v):
        if v.get("state") not in ("planned", "running", "paused"):
            raise hcdoc.Lost(f"state is {v.get('state')!r}; only a planned, running or paused experiment can be cancelled")
        v.update(state="cancelled", cancelled=True)
        return v

    sess = hcdoc.Session()
    try:
        sess.update(mname, mutate)
    finally:
        sess.close()
    print(json.dumps({"manifest": mname, "state": "cancelled",
                      "next": f"the orchestrator stops the workers and archives; if none runs: hcboard.py run {a.exp}"}))
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
        self.failed_here = 0  # jobs this orchestrator failed itself (crashed owner on its last attempt, exhausted)
        self.last_progress = 0
        self.children = set()
        self.worker_py = str(HERE / "docstore_worker.py")
        self.jobs = None  # last value read of every job doc
        self.changed = set()  # job docs with an event since then
        self.full = True  # the next reconcile re-reads every job doc
        self.reads = 0  # job docs read, for the `reconciled` event

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

    def restart_watch(self):
        """Resubscribe after the watcher died, waiting 1, 2, 4, 8, 10, 10 ... s between tries for about
        WATCH_RETRY_S in all (a core that restarts is not back at once), then give up with Down."""
        delay, end = 1, time.monotonic() + WATCH_RETRY_S
        while True:
            time.sleep(delay)
            try:
                return self.start_watch()
            except hcdoc.Down:
                if time.monotonic() + delay > end:
                    raise
                delay = min(delay * 2, 10)

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
                self.changed.add(n)
            elif re.match(r"^" + re.escape(hcdoc.PREFIX) + r"[a-z0-9_]+$", n) and n not in self.children:
                self.check_child(n)
        elif kind == "terminal" and m.get("type") in ("finished", "lost"):
            emit("worker_exit", terminal=m.get("terminal"), reason=m.get("type"))
            self.dirty = True
        elif kind == "snapshot" and m.get("reason") == "missed":
            emit("missed")
            self.dirty = self.full = True  # events were dropped: the cache can't be trusted

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
        """This experiment's terminals with name, state and exit_code. Some hc versions list slim rows
        (address, state, ... but no name or exit_code; see references/hc-facts.md): those are completed
        with `hc get`, or every worker would be named <exp>-w1 and every exit would count as a crash."""
        rows = hc_json(["ls", "--all", "--json", "--label", f"hco.board={self.exp}"])["terminals"]
        out = []
        for t in rows:
            if "name" not in t or (t.get("state") == "finished" and "exit_code" not in t):
                try:
                    t = {**t, **hc_json(["get", t["address"], "--json"])}
                except hcdoc.Down:
                    pass  # gone between ls and get, or get failed: keep the slim row
            out.append(t)
        return out

    def next_worker_name(self, terms):
        used = [int(m.group(1)) for t in terms if (m := re.match(rf"^{self.exp}-w(\d+)$", t.get("name") or ""))]
        return f"{self.exp}-w{max(used, default=0) + 1}"

    def start_worker(self, terms):
        name = self.next_worker_name(terms)
        try:
            r = subprocess.run(
                [HC, "run", "--bg", "--json", "--outlive-parent", "--no-notify", "--name", name,
                 "--label", f"exp={self.exp}", "--label", "role=worker", "--label", f"hco.board={self.exp}",
                 "--cwd", self.cwd, "--", sys.executable, self.worker_py, self.exp],
                capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            emit("worker_start_failed", name=name, error="hc run timed out after 60 s")
            return False
        if r.returncode != 0:
            emit("worker_start_failed", name=name, error=(r.stdout + r.stderr).strip()[:200])
            return False
        try:
            address = json.loads(r.stdout).get("address")
        except (ValueError, AttributeError):
            emit("worker_start_failed", name=name, error=f"hc run printed non-JSON: {r.stdout.strip()[:200]}")
            return False
        emit("worker_started", name=name, terminal=address)
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
        manifest, jobs = self.read_jobs()
        state = manifest.get("state")
        if state != self.last_state:
            emit("manifest_state", state=state)
            self.last_state = state
        if state == "cancelled":
            return self.cancel(terms, jobs)
        for n, v in orphans(jobs, alive):
            ops = requeue_ops(v["owner"], v["attempt"], v.get("max_attempts", 1), f"worker {v['owner']} died")
            try:
                _, jobs[n] = self.sess.update(n, lambda d, ops=ops: hcdoc.apply_ops(d, ops))
                emit("requeued", job=n, owner=v["owner"], attempt=v["attempt"], to=jobs[n]["state"])
                self.failed_here += jobs[n]["state"] == "failed"
            except hcdoc.Lost:
                jobs[n] = self.sess.get(n)[0]  # it moved on while we looked
        for n, v in exhausted(jobs):
            ops = [("if", "state", "pending"), ("set", "state", "failed"),
                   ("set", "result.error", "attempts exhausted"), ("now", "finished_ms", None)]
            try:
                _, jobs[n] = self.sess.update(n, lambda d, ops=ops: hcdoc.apply_ops(d, ops))
                self.failed_here += 1
            except hcdoc.Lost:
                jobs[n] = self.sess.get(n)[0]
        c = counts(jobs)
        if c != self.last_counts:
            progress = c["done"] + c["failed"] - self.failed_here  # failures we wrote ourselves are no progress
            if self.last_counts and progress > self.last_progress:
                self.bad_exits = 0  # progress: earlier crashes were not systematic
            emit("progress", **c)
            self.last_counts, self.last_progress = c, progress
        if c["missing"]:
            emit("warning", message=f"{c['missing']} job doc(s) missing", jobs=[n for n, v in jobs.items() if v is None])
        if self.bad_exits >= MAX_BAD_EXITS:
            raise GaveUp(f"{self.bad_exits} workers died with no job finishing; giving up (see `hc read` on a worker)")
        if c["pending"] + c["running"] == 0:
            if self.reads < len(jobs):  # concluded from cached docs: confirm with a full read before finishing
                self.full = True
                return self.reconcile()
            return True
        if state == "running":
            running_alive = sum(1 for v in jobs.values() if v and v.get("state") == "running" and v.get("owner") in alive)
            for _ in range(workers_to_start(manifest.get("max_workers", 1), c["pending"], running_alive, len(alive))):
                if not self.start_worker(terms):
                    break
        return False

    def read_jobs(self):
        """The manifest and every job doc. After the first full read, only docs that had an event are
        read again (a full read costs one round trip per job); `missed`, a lost watch and the --poll
        timer force a full read, so a dropped event is corrected within --poll seconds."""
        manifest, _ = self.sess.get(self.mname)
        if manifest is None:
            raise hcdoc.BadInput(f"{self.mname} does not exist")
        names = manifest.get("jobs") or []
        if self.full or self.jobs is None or set(names) != set(self.jobs):
            todo, self.jobs = names, {}
        else:
            todo = [n for n in names if n in self.changed]
        self.full = False
        self.changed.clear()  # before reading: an event during the reads below marks the doc again
        for n in todo:
            self.jobs[n] = self.sess.get(n)[0]
        self.reads = len(todo)
        return manifest, self.jobs  # the cache itself: reconcile's own writes land in it

    def cancel(self, terms, jobs):
        """The manifest says cancelled: stop this experiment's live workers (each kills its job's process
        group on SIGHUP), fail every job that has not finished, and let finalize export and archive."""
        for t in terms:
            if t.get("state") == "live":
                try:
                    subprocess.run([HC, "kill", t["address"]], capture_output=True, timeout=60)
                except subprocess.TimeoutExpired:
                    emit("warning", message=f"hc kill {t['address']} timed out after 60 s")
                    continue
                emit("worker_killed", terminal=t["address"], name=t.get("name"))
        n = 0
        for name, v in jobs.items():
            if v and v.get("state") not in FINAL:
                try:
                    self.sess.update(name, cancel_mutation)
                    n += 1
                except hcdoc.Lost:
                    pass  # it finished on its own just now
        emit("cancelled", jobs_failed=n)
        return True

    def preflight(self, manifest):
        """Fail before any job runs on what would otherwise only fail at the very end (the commit of the
        export) or on every single job (a job_cmd that can't start)."""
        if not os.path.isdir(self.cwd):
            raise hcdoc.BadInput(f"--cwd {self.cwd} is not a folder")
        cmd = manifest.get("job_cmd") or [""]
        prog = cmd[0]
        if isinstance(prog, str) and not PLACEHOLDER.search(prog):
            path = os.path.join(self.cwd, prog) if "/" in prog else shutil.which(prog)
            if not path or not os.path.isfile(path) or not os.access(path, os.X_OK):
                raise hcdoc.BadInput(f"job_cmd starts with {prog!r}, which is not an executable "
                                     f"{'file under ' + self.cwd if '/' in prog else 'on PATH'}; every job would fail")
        if not self.commit:
            return
        rel = os.path.join("experiments", f"{self.exp}.json")

        def g(*args):
            return subprocess.run(["git", "-C", self.cwd, *args], capture_output=True, text=True)

        if g("rev-parse", "--is-inside-work-tree").stdout.strip() != "true":
            raise hcdoc.BadInput(f"--cwd {self.cwd} is not a git repo, so the export could not be committed at "
                                 "the end: run in a repo, or pass --no-commit")
        if g("check-ignore", "-q", "--", rel).returncode == 0:
            raise hcdoc.BadInput(f"{rel} is ignored by git, so the export could not be committed at the end: "
                                 "un-ignore it, or pass --no-commit")
        if g("var", "GIT_COMMITTER_IDENT").returncode != 0:
            raise hcdoc.BadInput("git has no user.name / user.email here, so the export could not be committed "
                                 "at the end: set them, or pass --no-commit")

    # -- finish
    def finalize(self):
        mname, manifest, jobs = load(self.sess, self.exp)
        if manifest.get("state") not in ("done", "archived"):
            self.sess.update(mname, lambda v: hcdoc.apply_ops(v, [("set", "state", "done")]))
        out = os.path.join(self.cwd, "experiments", f"{self.exp}.json")
        res = self.sess.export(self.exp, out)
        if res["missing"] or res["jobs"] != len(manifest.get("jobs") or []):
            raise GaveUp(f"export is incomplete ({res}); docs kept")
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
        if c["failed"] and not manifest.get("cancelled"):
            print(f"{c['failed']} job(s) failed. After fixing the cause, rerun only those as a new experiment: "
                  f"hcboard.py init <new-id> --retry-failed {self.exp}")
        print(f"HCBOARD {self.exp} COMPLETE done={c['done']} failed={c['failed']}"
              + (" CANCELLED" if manifest.get("cancelled") else ""), flush=True)
        return hcdoc.OK

    def git_commit(self, path):
        rel = os.path.relpath(path, self.cwd)
        for args in (["add", "--", rel], ["commit", "-m", f"board: export experiment {self.exp}", "--", rel]):
            r = subprocess.run(["git", "-C", self.cwd, *args], capture_output=True, text=True)
            if r.returncode != 0:
                raise GaveUp(f"git {args[0]} failed ({(r.stdout + r.stderr).strip()[:200]}); docs kept, "
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
                try:
                    subprocess.run([HC, "rm", t["address"]], capture_output=True, timeout=60)
                except subprocess.TimeoutExpired:
                    emit("warning", message=f"hc rm {t['address']} timed out after 60 s")
                    continue
                n += 1
        return n

    # -- main loop
    def run(self):
        mname, manifest, _ = load(self.sess, self.exp)
        if manifest.get("state") == "archived":
            return self.resume_archived(mname, manifest)
        if manifest.get("state") in ("planned", "running", "paused"):
            self.preflight(manifest)
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
                    self.restart_watch()
                    self.dirty = self.full = True
                elif m:
                    self.on_message(m)
                now = time.monotonic()
                if (self.dirty and now - last >= MIN_RECONCILE_GAP) or now >= due:
                    self.full = self.full or now >= due  # the timer always re-reads everything
                    self.dirty = False
                    last, due = now, now + self.poll
                    if self.reconcile():
                        break
                if self.deadline and now > self.deadline:
                    raise GaveUp("--timeout reached; experiment left in place, rerun to continue")
        finally:
            self.stop_watch()
        return self.finalize()

    def resume_archived(self, mname, manifest):
        out = os.path.join(self.cwd, "experiments", f"{self.exp}.json")
        if not os.path.exists(out):
            raise GaveUp(f"{mname} is archived but {out} is missing; not deleting anything")
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
    p.add_argument("--spec", help="SPEC.json (optional with --retry-failed: then the parent's settings are reused)")
    p.add_argument("--parent")
    p.add_argument("--delta", help="JSON object describing what changed from the parent")
    p.add_argument("--retry-failed", metavar="PARENT",
                   help="make this experiment from PARENT's failed jobs only (read from its export)")
    p.add_argument("--export", help="PARENT's export file (default: <cwd>/experiments/PARENT.json)")
    p.add_argument("--cwd", default=os.getcwd(), help="where experiments/ is (default: here)")
    p = sub.add_parser("run")
    p.add_argument("exp")
    p.add_argument("--cwd", default=os.getcwd(), help="where workers run and experiments/<exp>.json is written (a git repo)")
    p.add_argument("--poll", type=float, default=30, help="reconcile at least this often, seconds (default 30)")
    p.add_argument("--timeout", type=float, help="give up after this many seconds (default: no limit)")
    p.add_argument("--no-commit", action="store_true", help="export but do not git commit (then docs are still deleted)")
    p = sub.add_parser("status")
    p.add_argument("exp")
    p.add_argument("--json", action="store_true")
    for name in ("pause", "resume", "cancel"):
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
        if a.cmd == "cancel":
            return cmd_cancel(a)
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
