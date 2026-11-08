#!/usr/bin/env python3
"""hcdoc: guarded access to herdr-core's versioned doc store (the experiment board).

  get    NAME                       print {"value":..., "version":N}
  set    NAME FILE|- --version N    write JSON from a file or stdin if the version still matches
  update NAME [--if P=JSON] [--set P=JSON] [--set-str P=TEXT] [--incr P] [--now P]
                                    read-modify-write with compare-and-set retry; --if is a
                                    precondition (exit 3 if false); P is a dotted path
  claim  NAME --owner ADDR          pending -> running, attempt += 1 (exit 0 won, 3 lost)
  delete NAME --version N           write null (the version is kept by core)
  watch  [--prefix P] [--label k=v] [--timeout S]
                                    stream doc and terminal events as JSON lines
  export EXP OUT.json               dump the manifest and every job doc of an experiment

Names must match ^[a-z0-9_-]{1,32}$ and start with the prefix (HCDOC_PREFIX, default "sk-");
`herdr-app` (the Herdr app's own doc) is never read or written. Values must be JSON objects with
"schema": 1 and at most ~1,000,000 bytes. Do not store secrets: the store is unencrypted.

Transport: `hc connect` (override with HCDOC_CONNECT, e.g. "ssh box hc connect"), else the core's
Unix socket ($HC_SOCKET, XDG_RUNTIME_DIR, /run/user/<uid>, then the state dir; HC_SESSION picks the
session). HCDOC_TRANSPORT=connect|socket forces one. HCDOC_TIMEOUT (default 30 s) bounds one reply.

Exit codes: 0 ok, 2 bad input, 3 conflict lost / claim lost / precondition false,
4 core unavailable or doc methods missing. Errors are one JSON object on stderr.

Python 3.8+, standard library only.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import random
import re
import select
import shlex
import socket
import subprocess
import sys
import time

OK, BAD, LOST, DOWN = 0, 2, 3, 4
PREFIX = os.environ.get("HCDOC_PREFIX", "sk-")
NAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
PATH_SEG_RE = re.compile(r"^[A-Za-z0-9_-]+$")
OWNER_RE = re.compile(r"^[A-Za-z0-9_./-]{1,64}$")
RESERVED = "herdr-app"
MAX_BYTES = 1_000_000  # core's limit is 1 MiB; keep a margin
CAS_RETRIES = 5
TIMEOUT = float(os.environ.get("HCDOC_TIMEOUT", "30"))
sleep = time.sleep  # tests replace this


class Fail(Exception):
    code = BAD


class BadInput(Fail):
    code = BAD


class Lost(Fail):
    code = LOST


class Down(Fail):
    code = DOWN


class Conflict(Exception):
    """doc.set refused: the version moved."""


# ------------------------------------------------------------------ validation


def check_prefix():
    if not NAME_RE.match(PREFIX) or RESERVED.startswith(PREFIX):
        raise BadInput(f"bad HCDOC_PREFIX {PREFIX!r}: must match {NAME_RE.pattern} and not cover {RESERVED}")


def check_name(name: str) -> str:
    check_prefix()
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise BadInput(f"bad doc name {name!r}: use 1-32 of a-z 0-9 - _")
    if name == RESERVED:
        raise BadInput(f"{RESERVED} belongs to the Herdr app; hcdoc never touches it")
    if not name.startswith(PREFIX):
        raise BadInput(f"doc name {name!r} must start with {PREFIX!r}")
    return name


def encoded_size(value) -> int:
    try:
        return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode())
    except (TypeError, ValueError) as e:
        raise BadInput(f"value is not strict JSON: {e}")


def check_value(value) -> None:
    if not isinstance(value, dict):
        raise BadInput("value must be a JSON object")
    if type(value.get("schema")) is not int or value["schema"] != 1:
        raise BadInput('value must contain "schema": 1')
    n = encoded_size(value)
    if n > MAX_BYTES:
        raise BadInput(f"value is {n} bytes (limit {MAX_BYTES}); store a file path in the doc instead")


def now_ms() -> int:
    return int(time.time() * 1000)


# ------------------------------------------------------------------- transport


class Conn:
    """JSON-RPC, one message per line, over `hc connect` or the core's Unix socket."""

    def __init__(self):
        self.proc = None
        self.rfd = None  # `hc connect` stdout
        self.wpipe = None  # `hc connect` stdin
        self.sock = None
        self.buf = b""
        self.next_id = 0
        self.notes = []  # notifications that arrived while waiting for a reply

    # -- open
    @classmethod
    def open(cls):
        mode = os.environ.get("HCDOC_TRANSPORT", "auto")
        if mode not in ("auto", "connect", "socket"):
            raise BadInput("HCDOC_TRANSPORT must be auto, connect or socket")
        errors = []
        for how in (("connect", "socket") if mode == "auto" else (mode,)):
            c = cls()
            try:
                c._start_connect() if how == "connect" else c._start_socket()
                c._hello()
                return c
            except Fail as e:
                c.close()
                errors.append(str(e))
                if how == "connect" and "doc methods" in str(e):
                    raise  # reached a core that lacks the feature: the socket would say the same
        raise Down("; ".join(errors))

    def _start_connect(self):
        cmd = shlex.split(os.environ.get("HCDOC_CONNECT", "hc connect"))
        try:
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, bufsize=0)
        except OSError as e:
            raise Down(f"cannot run {cmd[0]}: {e}")
        self.rfd, self.wpipe = self.proc.stdout, self.proc.stdin

    def _start_socket(self):
        path = socket_path()
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(TIMEOUT)
        try:
            s.connect(path)
        except OSError as e:
            s.close()
            raise Down(f"cannot connect to {path}: {e}")
        self.sock = s

    def _hello(self):
        res = self.call("hello")
        methods = res.get("methods") or []
        if "doc.get" not in methods or "doc.set" not in methods:
            raise Down("this core has no doc methods (doc.get/doc.set); fall back to the old behavior")

    # -- io
    def _send(self, data: bytes):
        try:
            if self.sock:
                self.sock.sendall(data)
            else:
                self.wpipe.write(data)
        except (OSError, ValueError) as e:
            raise Down(f"core connection closed: {e}")

    def _recv(self, timeout):
        """bytes, b'' at EOF, None on timeout."""
        try:
            if self.sock:
                r, _, _ = select.select([self.sock], [], [], timeout)
                return self.sock.recv(65536) if r else None
            r, _, _ = select.select([self.rfd], [], [], timeout)
            return os.read(self.rfd.fileno(), 65536) if r else None
        except (OSError, ValueError) as e:
            raise Down(f"core connection closed: {e}")

    def readmsg(self, timeout):
        """Next JSON message, or None on timeout. Raises Down at EOF."""
        deadline = time.monotonic() + timeout
        while b"\n" not in self.buf:
            chunk = self._recv(max(0.0, deadline - time.monotonic()))
            if chunk is None:
                return None
            if chunk == b"":
                raise Down("core connection closed")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        try:
            return json.loads(line)
        except ValueError:
            raise Down(f"core sent a non-JSON line: {line[:80]!r}")

    def call(self, method, **params):
        self.next_id += 1
        rid = self.next_id
        self._send((json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}) + "\n").encode())
        deadline = time.monotonic() + TIMEOUT
        while True:
            msg = self.readmsg(max(0.0, deadline - time.monotonic()))
            if msg is None:
                raise Down(f"no reply to {method} within {TIMEOUT:g}s")
            if msg.get("id") != rid:
                if "method" in msg:
                    self.notes.append(msg)
                continue
            if "error" in msg:
                raise rpc_error(method, msg["error"])
            return msg.get("result")

    def close(self):
        try:
            if self.sock:
                self.sock.close()
            if self.proc:
                self.wpipe.close()
                try:
                    self.proc.wait(2)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait()
        except (OSError, ValueError):
            pass


def rpc_error(method, err):
    kind = (err.get("data") or {}).get("kind")
    msg = f"{method}: {err.get('message', err)}"
    if kind == "conflict":
        return Conflict(msg)
    if kind == "invalid_params":
        return BadInput(msg)
    return Down(msg)  # unavailable, needs_update, machine_unknown, internal, ...


def socket_path() -> str:
    env = os.environ.get("HC_SOCKET")
    if env:
        return env
    s = os.environ.get("HC_SESSION") or "default"
    state = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    cands = []
    if os.environ.get("XDG_RUNTIME_DIR"):
        cands.append(f"{os.environ['XDG_RUNTIME_DIR']}/herdr-core/{s}.sock")
    cands += [f"/run/user/{os.getuid()}/herdr-core/{s}.sock", f"{state}/herdr-core/{s}/{s}.sock"]
    for c in cands:
        if os.path.exists(c):
            return c
    raise Down("no core socket found (tried " + ", ".join(cands) + ")")


# ------------------------------------------------------------------- doc access


def doc_get(conn, name):
    r = conn.call("doc.get", name=check_name(name))
    return r["value"], r["version"]


def doc_set(conn, name, value, version):
    """Write once. Returns the new version; raises Conflict if the version moved."""
    check_name(name)
    if value is not None:
        check_value(value)
    return conn.call("doc.set", name=name, value=value, version=version)["version"]


def cas_update(conn, name, mutate):
    """Read, apply mutate(value) -> new value, write. On conflict re-read and retry.

    mutate may raise Lost (precondition false / claim lost) or BadInput. Gives up after
    CAS_RETRIES retries with 50-300 ms jitter and raises Lost, loudly.
    """
    for attempt in range(CAS_RETRIES + 1):
        value, version = doc_get(conn, name)
        if value is None:
            raise BadInput(f"{name} does not exist (use `set --version 0` to create it)")
        new = mutate(copy.deepcopy(value))
        try:
            return doc_set(conn, name, new, version), new
        except Conflict:
            if attempt < CAS_RETRIES:
                sleep(random.uniform(0.05, 0.3))
    raise Lost(f"{name}: still conflicting after {CAS_RETRIES} retries; giving up")


class Session:
    """Doc access over one connection that is reopened once if the core connection drops."""

    def __init__(self):
        self.conn = None

    def _do(self, fn):
        for tries in (0, 1):
            try:
                if self.conn is None:
                    self.conn = Conn.open()
                return fn(self.conn)
            except Down:
                self.close()
                if tries:
                    raise
                sleep(1)

    def get(self, name):
        return self._do(lambda c: doc_get(c, name))

    def set(self, name, value, version):
        return self._do(lambda c: doc_set(c, name, value, version))

    def update(self, name, mutate):
        return self._do(lambda c: cas_update(c, name, mutate))

    def export(self, exp, out_path):
        return self._do(lambda c: export_experiment(c, exp, out_path))

    def close(self):
        if self.conn:
            self.conn.close()
        self.conn = None


# --------------------------------------------------------------------- mutations


def split_path(path):
    segs = path.split(".")
    if not all(PATH_SEG_RE.match(s) for s in segs):
        raise BadInput(f"bad path {path!r}: dotted names of A-Z a-z 0-9 - _")
    return segs


def path_get(value, path):
    cur = value
    for s in split_path(path):
        if not isinstance(cur, dict) or s not in cur:
            return None
        cur = cur[s]
    return cur


def path_set(value, path, new):
    segs = split_path(path)
    cur = value
    for s in segs[:-1]:
        nxt = cur.get(s)
        if nxt is None:
            nxt = cur[s] = {}
        elif not isinstance(nxt, dict):
            raise BadInput(f"cannot set {path}: {s} is not an object")
        cur = nxt
    cur[segs[-1]] = new


def apply_ops(value, ops):
    """ops: list of (kind, path, arg). All `if` ops are checked first."""
    for kind, path, arg in ops:
        if kind == "if" and path_get(value, path) != arg:
            raise Lost(f"precondition false: {path} is {json.dumps(path_get(value, path))}, wanted {json.dumps(arg)}")
    for kind, path, arg in ops:
        if kind == "set":
            path_set(value, path, arg)
        elif kind == "incr":
            cur = path_get(value, path)
            if cur is None:
                cur = 0
            if type(cur) is not int:
                raise BadInput(f"cannot --incr {path}: it is {json.dumps(cur)}")
            path_set(value, path, cur + 1)
        elif kind == "now":
            path_set(value, path, now_ms())
    return value


def parse_assign(text, as_string):
    if "=" not in text:
        raise BadInput(f"expected PATH=VALUE, got {text!r}")
    path, raw = text.split("=", 1)
    split_path(path)
    if as_string:
        return path, raw
    try:
        return path, json.loads(raw)
    except ValueError:
        raise BadInput(f"{path}: {raw!r} is not JSON (use the -str form for plain text)")


def claim_mutation(owner):
    def mutate(v):
        if v.get("state") != "pending":
            raise Lost(f"not pending (state is {json.dumps(v.get('state'))})")
        attempt, mx = v.get("attempt", 0), v.get("max_attempts")
        if type(attempt) is not int or (mx is not None and (type(mx) is not int or attempt >= mx)):
            raise Lost(f"attempts exhausted ({attempt} of {mx})")
        v.update(state="running", owner=owner, attempt=attempt + 1, started_ms=now_ms(), finished_ms=None)
        return v
    return mutate


# ---------------------------------------------------------------------- commands


def out(obj):
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def read_json_arg(src):
    try:
        text = sys.stdin.read() if src == "-" else open(src, encoding="utf-8").read()
    except OSError as e:
        raise BadInput(f"cannot read {src}: {e}")
    try:
        return json.loads(text)
    except ValueError as e:
        raise BadInput(f"{src} is not JSON: {e}")


def cmd_get(a, conn):
    value, version = doc_get(conn, a.name)
    out({"value": value, "version": version})


def cmd_set(a, conn):
    value = read_json_arg(a.file)
    if value is None:
        raise BadInput("use `delete` to write null")
    try:
        out({"version": doc_set(conn, a.name, value, a.version)})
    except Conflict as e:
        raise Lost(str(e))


def cmd_update(a, conn):
    if not a.ops:
        raise BadInput("nothing to do: give --if/--set/--set-str/--incr/--now")
    version, value = cas_update(conn, check_name(a.name), lambda v: apply_ops(v, a.ops))
    out({"name": a.name, "version": version, "value": value})


def cmd_claim(a, conn):
    if not OWNER_RE.match(a.owner):
        raise BadInput(f"bad --owner {a.owner!r}: expected a terminal address like t7 or machine/t7")
    version, value = cas_update(conn, check_name(a.name), claim_mutation(a.owner))
    out({"name": a.name, "version": version, "value": value})


def cmd_delete(a, conn):
    try:
        out({"version": doc_set(conn, a.name, None, a.version)})
    except Conflict as e:
        raise Lost(str(e))


def export_experiment(conn, exp, out_path):
    """Write manifest + every job doc of experiment `exp` to out_path (atomically). Returns a summary."""
    mname = check_name(PREFIX + exp)
    manifest, _ = doc_get(conn, mname)
    if manifest is None:
        raise BadInput(f"manifest {mname} does not exist")
    jobs, missing = {}, []
    for jname in manifest.get("jobs") or []:
        if not isinstance(jname, str) or not jname.startswith(mname + "-j"):
            raise BadInput(f"manifest lists {jname!r}, which does not belong to {mname}; not exporting")
        v, _ = doc_get(conn, jname)
        if v is None:
            missing.append(jname)
        else:
            jobs[jname] = v
    dump = {"schema": 1, "exp": exp, "exported_ms": now_ms(), "manifest": manifest,
            "jobs": jobs, "missing": missing}
    tmp = out_path + ".tmp"
    try:
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(dump, f, indent=1, sort_keys=True)
            f.write("\n")
        os.replace(tmp, out_path)
    except OSError as e:
        raise BadInput(f"cannot write {out_path}: {e}")
    return {"out": out_path, "jobs": len(jobs), "missing": missing}


def cmd_export(a, conn):
    out(export_experiment(conn, a.exp, a.out))


def term_view(t):
    return {k: t.get(k) for k in ("address", "name", "state", "exit_code", "labels")}


def labels_match(labels, want):
    return all((labels or {}).get(k) == v for k, v in want.items())


def cmd_watch(a, conn):
    """One connection, subscribed unfiltered (core's filters would hide doc events); we filter here."""
    check_prefix()
    prefix = a.prefix or PREFIX
    if not prefix.startswith(PREFIX):
        raise BadInput(f"--prefix {prefix!r} must start with {PREFIX!r}")
    want = {}
    for item in a.label or []:
        if "=" not in item:
            raise BadInput(f"bad --label {item!r}: expected k=v")
        k, v = item.split("=", 1)
        want[k] = v
    mine = set()  # addresses of terminals whose labels match

    def snapshot(reason, res):
        mine.clear()
        view = []
        for t in res.get("terminals") or []:
            if labels_match(t.get("labels"), want):
                mine.add(t.get("address"))
                view.append(term_view(t))
        out({"kind": "snapshot", "reason": reason, "seq": res.get("seq"), "terminals": view})

    def handle(m):
        meth, p = m.get("method"), m.get("params") or {}
        if meth == "heartbeat":
            out({"kind": "heartbeat", "seq": p.get("seq")})
        elif meth == "missed":
            snapshot("missed", p)
        elif meth == "event":
            typ, data = p.get("type"), p.get("data") or {}
            if typ == "doc":
                if str(data.get("name", "")).startswith(prefix):
                    out({"kind": "doc", "name": data["name"], "version": data.get("version"), "seq": p.get("seq")})
                return
            addr = p.get("terminal") or data.get("address")
            if typ == "created" and labels_match(data.get("labels"), want):
                mine.add(addr)
            elif typ == "labels" and "labels" in data:
                (mine.add if labels_match(data["labels"], want) else mine.discard)(addr)
            if addr in mine or not want:
                out({"kind": "terminal", "type": typ, "terminal": addr, "seq": p.get("seq"), "data": data})
            if typ == "removed":
                mine.discard(addr)

    res = conn.call("events.subscribe")
    snapshot("subscribe", res)
    for m in conn.notes:
        handle(m)
    conn.notes.clear()
    end = time.monotonic() + a.timeout if a.timeout else None
    parent = os.getppid()
    while end is None or time.monotonic() < end:
        if a.exit_with_parent and os.getppid() != parent:
            return  # whoever read our output is gone
        m = conn.readmsg(1.0 if end is None else max(0.0, min(1.0, end - time.monotonic())))
        if m is not None:
            handle(m)


COMMANDS = {"get": cmd_get, "set": cmd_set, "update": cmd_update, "claim": cmd_claim,
            "delete": cmd_delete, "export": cmd_export, "watch": cmd_watch}


class OpAction(argparse.Action):
    def __init__(self, option_strings, dest, kind=None, as_string=False, **kw):
        self.kind, self.as_string = kind, as_string
        super().__init__(option_strings, dest, **kw)

    def __call__(self, parser, ns, values, option_string=None):
        ops = list(getattr(ns, "ops", None) or [])
        try:
            if self.kind in ("incr", "now"):
                split_path(values)
                ops.append((self.kind, values, None))
            else:
                path, val = parse_assign(values, self.as_string)
                ops.append((self.kind, path, val))
        except Fail as e:
            parser.error(f"{option_string}: {e}")  # exits 2
        ns.ops = ops


def build_parser():
    ap = argparse.ArgumentParser(prog="hcdoc", description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("get")
    p.add_argument("name")
    p = sub.add_parser("set")
    p.add_argument("name")
    p.add_argument("file", help="JSON file, or - for stdin")
    p.add_argument("--version", type=int, required=True, help="version you read (0 to create)")
    p = sub.add_parser("update")
    p.add_argument("name")
    p.add_argument("--if", dest="ops", action=OpAction, kind="if", metavar="PATH=JSON")
    p.add_argument("--if-str", dest="ops", action=OpAction, kind="if", as_string=True, metavar="PATH=TEXT")
    p.add_argument("--set", dest="ops", action=OpAction, kind="set", metavar="PATH=JSON")
    p.add_argument("--set-str", dest="ops", action=OpAction, kind="set", as_string=True, metavar="PATH=TEXT")
    p.add_argument("--incr", dest="ops", action=OpAction, kind="incr", metavar="PATH")
    p.add_argument("--now", dest="ops", action=OpAction, kind="now", metavar="PATH",
                   help="set PATH to the current time in ms")
    p = sub.add_parser("claim")
    p.add_argument("name")
    p.add_argument("--owner", required=True, help="your terminal address, $HC_TERMINAL_ID")
    p = sub.add_parser("delete")
    p.add_argument("name")
    p.add_argument("--version", type=int, required=True)
    p = sub.add_parser("watch")
    p.add_argument("--prefix", help=f"only docs starting with this (default {PREFIX!r})")
    p.add_argument("--label", action="append", metavar="k=v",
                   help="only terminals with this label (repeatable); docs are unaffected")
    p.add_argument("--timeout", type=float, help="stop after this many seconds (default: run until the core goes away)")
    p.add_argument("--exit-with-parent", action="store_true", help="stop when the process that started us exits")
    p = sub.add_parser("export")
    p.add_argument("exp", help="experiment id, e.g. e051")
    p.add_argument("out", help="output JSON file")
    return ap


def main(argv=None):
    a = build_parser().parse_args(argv)
    conn = None
    try:
        # validate offline first so bad input never needs a core
        if hasattr(a, "name"):
            check_name(a.name)
        conn = Conn.open()
        COMMANDS[a.cmd](a, conn)
        return OK
    except Conflict as e:  # a bare conflict that no command translated
        err = Lost(str(e))
    except Fail as e:
        err = e
    except KeyboardInterrupt:
        return OK
    finally:
        if conn:
            conn.close()
    sys.stderr.write(json.dumps({"error": str(err), "code": err.code}) + "\n")
    return err.code


if __name__ == "__main__":
    sys.exit(main())
