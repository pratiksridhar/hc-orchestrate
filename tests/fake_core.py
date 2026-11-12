#!/usr/bin/env python3
"""Stand-in for the hc core, for offline board tests (test_hcboard_offline.py). No real hc is involved.

Two faces, one state folder ($FAKE_CORE_DIR):
  fake_core.py connect            JSON-RPC lines on stdin/stdout, like `hc connect` (HCDOC_CONNECT):
                                  hello, doc.get, doc.set (compare-and-set), events.subscribe
  fake_core.py ls|get|run|kill|rm the `hc` commands the board uses (HCBOARD_HC)

Shapes follow references/hc-docstore-notes.md (A1-A12): a never-written doc is {value: null, version: 0};
a stale doc.set fails with data.kind "conflict"; a null write keeps counting versions; events are
{"method":"event","params":{"type","data","seq"[,"terminal"]}}. `run` really starts the program, in the
background, with HC_TERMINAL_ID set; `kill` sends it SIGHUP like `hc kill` (signal 1).

Knobs: FAKE_CORE_SLIM=1 makes `ls` return slim rows (no name, labels or exit_code), as references/hc-facts.md
describes them. While $FAKE_CORE_DIR/down exists, a new `connect` exits at once (a core that is not back yet).
Every doc.get/doc.set is counted in ops.json ({"get": n, "set": n, "get:<name>": n}).
"""
import fcntl
import json
import os
import select
import signal
import subprocess
import sys
import time

DIR = os.environ.get("FAKE_CORE_DIR") or sys.exit("fake_core: FAKE_CORE_DIR is not set")


class Locked:
    """Read-modify-write of one JSON file under an exclusive lock."""

    def __init__(self, name, default):
        self.path, self.default = os.path.join(DIR, name), default

    def __enter__(self):
        self.lock = open(self.path + ".lock", "w")
        fcntl.flock(self.lock, fcntl.LOCK_EX)
        try:
            with open(self.path) as f:
                self.data = json.load(f)
        except FileNotFoundError:
            self.data = self.default
        return self.data

    def __exit__(self, *exc):
        if exc[0] is None:
            tmp = f"{self.path}.{os.getpid()}"
            with open(tmp, "w") as f:
                json.dump(self.data, f)
            os.replace(tmp, self.path)
        self.lock.close()


def event(typ, data, terminal=None):
    with open(os.path.join(DIR, "events.jsonl"), "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(json.dumps({"type": typ, "data": data, **({"terminal": terminal} if terminal else {})}) + "\n")


def count(*keys):
    with Locked("ops.json", {}) as ops:
        for k in keys:
            ops[k] = ops.get(k, 0) + 1


# ------------------------------------------------------------------ connect


def connect():
    if os.path.exists(os.path.join(DIR, "down")):
        sys.exit(1)  # the core is away: new connections fail, open ones keep working
    sub_at = None  # byte offset in events.jsonl from which this subscriber still has to read
    seq = 0
    buf = b""
    epath = os.path.join(DIR, "events.jsonl")

    def send(obj):
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()

    def reply(rid, result=None, error=None):
        send({"jsonrpc": "2.0", "id": rid, **({"error": error} if error else {"result": result})})

    def handle(msg):
        nonlocal sub_at
        rid, meth, p = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if meth == "hello":
            reply(rid, {"methods": ["doc.get", "doc.set", "events.subscribe"], "features": ["events"]})
        elif meth == "doc.get":
            count("get", "get:" + p["name"])
            with Locked("docs.json", {}) as docs:
                d = docs.get(p["name"], {"value": None, "version": 0})
            reply(rid, d)
        elif meth == "doc.set":
            count("set")
            with Locked("docs.json", {}) as docs:
                cur = docs.get(p["name"], {"value": None, "version": 0})
                if cur["version"] != p["version"]:
                    return reply(rid, error={"code": -32000, "data": {"kind": "conflict"},
                                             "message": f'document "{p["name"]}" is at version {cur["version"]}, '
                                                        f'not {p["version"]}'})
                docs[p["name"]] = {"value": p["value"], "version": cur["version"] + 1}
                event("doc", {"name": p["name"], "version": cur["version"] + 1})
            reply(rid, {"version": cur["version"] + 1})
        elif meth == "events.subscribe":
            sub_at = os.path.getsize(epath) if os.path.exists(epath) else 0
            with Locked("terminals.json", {"n": 0, "terminals": {}}) as st:
                terms = list(st["terminals"].values())
            reply(rid, {"channel": "c1", "seq": 0, "terminals": terms})
        else:
            reply(rid, error={"code": -32601, "message": f"fake core: no method {meth}"})

    while True:
        r, _, _ = select.select([sys.stdin], [], [], 0.05)
        if r:
            chunk = os.read(sys.stdin.fileno(), 65536)
            if not chunk:
                return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                handle(json.loads(line))
        if sub_at is not None and os.path.exists(epath) and os.path.getsize(epath) > sub_at:
            with open(epath, "rb") as f:
                f.seek(sub_at)
                data = f.read()
            done = data.rfind(b"\n") + 1  # only whole lines
            sub_at += done
            for line in data[:done].splitlines():
                seq += 1
                send({"method": "event", "params": {**json.loads(line), "seq": seq}})


# ------------------------------------------------------------------ hc commands


def fail(kind, msg, code):
    print(json.dumps({"error": {"kind": kind, "message": msg, "hint": ""}}), file=sys.stderr)
    sys.exit(code)


def find(st, ref):
    for addr, t in st["terminals"].items():
        if ref in (addr, t.get("name")):
            return addr, t
    fail("not_found", f"no terminal {ref}", 3)


def hc(argv):
    cmd = argv[0]
    if cmd == "ls":
        want = [tuple(argv[i + 1].split("=", 1)) for i, a in enumerate(argv) if a == "--label"]
        with Locked("terminals.json", {"n": 0, "terminals": {}}) as st:
            rows = [t for t in st["terminals"].values()
                    if all(t["labels"].get(k) == v for k, v in want) and (t["state"] == "live" or "--all" in argv)]
        if os.environ.get("FAKE_CORE_SLIM"):
            rows = [{"address": t["address"], "agent": "x", "cwd": t["cwd"], "state": t["state"], "turn": 0,
                     "where": "w"} for t in rows]
        print(json.dumps({"hidden_finished": 0, "terminals": rows}))
    elif cmd == "get":
        with Locked("terminals.json", {"n": 0, "terminals": {}}) as st:
            print(json.dumps(find(st, argv[1])[1]))
    elif cmd == "run":
        opts, labels, i = {}, {}, 1
        while argv[i] != "--":
            if argv[i] in ("--name", "--cwd"):
                opts[argv[i]], i = argv[i + 1], i + 2
            elif argv[i] == "--label":
                k, v = argv[i + 1].split("=", 1)
                labels[k], i = v, i + 2
            else:
                i += 1
        prog = argv[i + 1:]
        with Locked("terminals.json", {"n": 0, "terminals": {}}) as st:
            if any(t["name"] == opts.get("--name") and t["state"] == "live" for t in st["terminals"].values()):
                fail("name_taken", f"a live terminal is already named {opts.get('--name')}", 6)
            st["n"] += 1
            addr = f"t{st['n']}"
            t = {"address": addr, "name": opts.get("--name"), "labels": labels, "cwd": opts.get("--cwd"),
                 "state": "live", "exit_code": None, "pid": None}
            st["terminals"][addr] = t
        event("created", {"address": addr, "labels": labels, "name": t["name"]}, addr)
        log = open(os.path.join(DIR, f"{addr}.log"), "wb")
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "_hold", addr, *prog], cwd=t["cwd"],
                         stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                         env={**os.environ, "HC_TERMINAL_ID": addr})
        print(json.dumps(t))
    elif cmd == "_hold":  # the "terminal": runs the program, records how it ended
        addr, prog = argv[1], argv[2:]
        child = subprocess.Popen(prog)
        with Locked("terminals.json", {}) as st:
            st["terminals"][addr]["pid"] = child.pid
        rc = child.wait()
        code, sig = (rc, None) if rc >= 0 else (None, -rc)
        with Locked("terminals.json", {}) as st:
            if addr in st["terminals"]:
                st["terminals"][addr].update(state="finished", exit_code=code, signal=sig)
        event("finished", {"address": addr, "exit_code": code, "signal": sig}, addr)
    elif cmd == "kill":
        with Locked("terminals.json", {}) as st:
            addr, t = find(st, argv[1])
        if t["state"] != "live":
            fail("finished", "already finished", 11)
        for _ in range(50):  # the program may not have started yet
            with Locked("terminals.json", {}) as st:
                pid = st["terminals"][addr]["pid"]
            if pid:
                break
            time.sleep(0.05)
        try:
            os.kill(pid, signal.SIGHUP)
        except (OSError, TypeError):
            pass
        print(json.dumps({"address": addr}))
    elif cmd == "rm":
        with Locked("terminals.json", {}) as st:
            addr, _ = find(st, argv[1])
            del st["terminals"][addr]
        event("removed", {"address": addr}, addr)
    else:
        fail("unsupported", f"fake core: {argv}", 2)


if __name__ == "__main__":
    connect() if sys.argv[1] == "connect" else hc(sys.argv[1:])
