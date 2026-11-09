#!/usr/bin/env python3
"""Stand-in for `hc`, used by test_hco.py via HCO_HC.

Mirrors the shapes of the REAL hc 0.1.17, verified live:
  - `ls --json`  -> slim rows {address, agent, cwd, state, turn, where[, status: "<word>"]}
  - `get --json` -> full record (name, labels, status {state,kind?}, reported, exit_code, ...)
State lives in $FAKE_HC_STATE (a JSON file). Knobs (env): FAKE_HC_TEMPLATES (comma list),
FAKE_HC_NOT_SENT=1 (templated run reports first_message.sent=false), FAKE_HC_RUN_FAIL=1 (`run` errors),
FAKE_HC_RUN_DELAY=<s> (`run` takes that long), FAKE_HC_SEND_FAIL=1 (`send` is refused as blocked),
FAKE_HC_NO_CHILDREN=1 (`wait --children` errors needs_target), FAKE_HC_CHECK_SLOW=1 (a check terminal
stays live). A terminal labeled hco.check is a plain program: `wait <t>` runs its argv and finishes it.
`send` appends [target, text] to state["sent"].
"""
import json
import os
import sys

STATE = os.environ["FAKE_HC_STATE"]


def load():
    try:
        return json.load(open(STATE))
    except FileNotFoundError:
        return {"n": 10, "terminals": {}}


def save(s):
    tmp = f"{STATE}.{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(s, f)
    os.replace(tmp, STATE)


def err(kind, msg, code):
    print(json.dumps({"error": {"kind": kind, "message": msg, "hint": ""}}), file=sys.stderr)
    sys.exit(code)


def find(s, ref):
    ref = ref.split("/")[-1]  # machine/t7 or machine/name: every machine is this one here
    for addr, t in s["terminals"].items():
        if ref in (addr, t.get("name")):
            return addr, t
    err("not_found", f"no terminal {ref}", 3)


def labels_match(t, wanted):
    return all(t["labels"].get(k) == v for k, v in wanted)


def main(argv):
    if argv[0] == "run" and os.environ.get("FAKE_HC_RUN_DELAY"):
        import time
        time.sleep(float(os.environ["FAKE_HC_RUN_DELAY"]))
    import fcntl
    lock = open(STATE + ".lock", "w")
    fcntl.flock(lock, fcntl.LOCK_EX)  # like the real core: one request at a time
    s = load()
    if argv[0] == "--version":
        print("hc 0.1.17")
    elif argv[0] == "templates":
        names = [n for n in os.environ.get("FAKE_HC_TEMPLATES", "").split(",") if n]
        print(json.dumps({"templates": [{"name": n, "command": "x"} for n in names]}))
    elif argv[0] == "ls":
        want = [tuple(argv[i + 1].split("=", 1)) for i, a in enumerate(argv) if a == "--label"]
        rows = []
        for addr, t in s["terminals"].items():
            if t["state"] != "live" and "--all" not in argv:
                continue
            if not labels_match(t, want):
                continue
            row = {"address": addr, "agent": "x", "cwd": t["cwd"], "state": t["state"], "turn": 0, "where": "w"}
            if t.get("status"):
                row["status"] = t["status"]["state"]  # slim: bare string
            rows.append(row)
        print(json.dumps({"hidden_finished": 0, "terminals": rows}))
    elif argv[0] == "get":
        _, t = find(s, argv[1])
        print(json.dumps(t))
    elif argv[0] == "run":
        opts, labels, i = {}, {}, 1
        while i < len(argv) and argv[i] != "--":
            a = argv[i]
            if a in ("--name", "--cwd", "--on"):
                opts[a], i = argv[i + 1], i + 2
            elif a == "--label":
                k, v = argv[i + 1].split("=", 1)
                labels[k], i = v, i + 2
            else:
                i += 1
        prog = argv[i + 1:]
        if os.environ.get("FAKE_HC_RUN_FAIL"):
            err("internal", "could not start the program", 1)
        s["n"] += 1
        addr = f"t{s['n']}"
        plain = "hco.check" in labels  # a check is a plain program: no OSC 7501 status
        t = {"address": addr, "name": opts.get("--name"), "labels": labels, "cwd": opts.get("--cwd"),
             "state": "live", "status": None if plain else {"state": "working"}, "turn": 0 if plain else 1,
             "reported": not plain,
             "waiting_for_person": False, "exit_code": None, "argv": prog, "parent": os.environ.get("HC_TERMINAL_ID")}
        s["terminals"][addr] = t
        save(s)
        res = dict(t)
        if prog and prog[0] in os.environ.get("FAKE_HC_TEMPLATES", "").split(","):
            if os.environ.get("FAKE_HC_NOT_SENT"):
                res["first_message"] = {"sent": False, "reason": "agent not ready"}
            else:
                res["first_message"] = {"sent": True, "started": True, "turn": 1}
        print(json.dumps(res))
        return
    elif argv[0] == "exec":  # like real `hc exec --json`; --on is ignored (runs here)
        import base64
        import subprocess
        sep = argv.index("--")
        if os.environ.get("FAKE_HC_EXEC_DOWN"):
            err("unreachable", "machine is not connected", 5)
        stdin = sys.stdin.read() if "-i" in argv[:sep] else None
        p = subprocess.run(argv[sep + 1:], input=stdin, capture_output=True, text=True)
        print(json.dumps({"code": p.returncode, "signal": None, "timed_out": False, "truncated": False,
                          "stdout": base64.b64encode(p.stdout.encode()).decode(),
                          "stderr": base64.b64encode(p.stderr.encode()).decode()}))
        return
    elif argv[0] == "file":  # file read|write [--on M] PATH [--json]; --on is ignored
        path = [a for i, a in enumerate(argv[2:], 2) if not a.startswith("--") and argv[i - 1] != "--on"][0]
        if argv[1] == "write":
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, "w").write(sys.stdin.read())
            print(json.dumps({"path": path}))
        else:
            if not os.path.isfile(path):
                err("not_found", f"no file {path}", 3)
            sys.stdout.write(open(path).read())
        return
    elif argv[0] == "wait" and argv[1] != "--children":  # wait <t>: a check terminal runs to its end here
        import subprocess
        addr, t = find(s, argv[1])
        if t["state"] == "live" and "hco.check" in t["labels"] and not os.environ.get("FAKE_HC_CHECK_SLOW"):
            p = subprocess.run(t["argv"], cwd=t["cwd"], capture_output=True, text=True)
            t["state"], t["exit_code"] = "finished", p.returncode
        ended = t["state"] != "live"
        print(json.dumps({"terminal": addr, "ended": ended, "reason": "exited" if ended else "timeout",
                          "exit_code": t.get("exit_code"), "children": []}))
    elif argv[0] == "wait":
        if os.environ.get("FAKE_HC_NO_CHILDREN"):
            err("needs_target", "this terminal has no children", 7)
        print(json.dumps({"terminal": None, "ended": False, "reason": "timeout", "children": []}))
    elif argv[0] == "send":
        addr, t = find(s, argv[1])
        if t["state"] != "live":
            err("finished", "that terminal has finished", 11)
        if os.environ.get("FAKE_HC_SEND_FAIL"):
            err("blocked", "the program is waiting for a person", 9)
        s.setdefault("sent", []).append([addr, argv[2]])
    elif argv[0] == "kill":
        addr, t = find(s, argv[1])
        if t["state"] != "live":
            err("finished", "already finished", 11)
        t["state"], t["status"] = "finished", None
        save(s)
        print(json.dumps({"address": addr}))
    elif argv[0] == "rm":
        addr, _ = find(s, argv[1])
        del s["terminals"][addr]
        save(s)
        print(json.dumps({"address": addr}))
    else:
        err("unsupported", f"fake hc: {argv}", 2)
    save(s)


main(sys.argv[1:])
