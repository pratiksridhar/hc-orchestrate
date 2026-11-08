"""Offline tests for scripts/hcdoc.py (no hc needed). Run: python3 -I -m unittest discover -s tests -v

The doc store is faked in memory; the "feature missing" test uses a tiny fake core on stdin/stdout.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
HCDOC = Path(os.environ.get("HCDOC_SCRIPT") or HERE.parent / "scripts" / "hcdoc.py")
sys.path.insert(0, str(HCDOC.parent))
import hcdoc  # noqa: E402


class FakeConn:
    """In-memory doc.get/doc.set. `rivals` = writes that sneak in before our next N doc.set calls."""

    def __init__(self, docs=None, rivals=0):
        self.docs = {k: (v, 1) for k, v in (docs or {}).items()}
        self.rivals = rivals
        self.sets = 0

    def call(self, method, **p):
        name = p["name"]
        value, ver = self.docs.get(name, (None, 0))
        if method == "doc.get":
            return {"value": value, "version": ver}
        self.sets += 1
        if self.rivals > 0:  # someone else writes first
            self.rivals -= 1
            self.docs[name] = (value, ver + 1)
            ver += 1
            raise hcdoc.Conflict("moved")
        if p["version"] != ver:
            raise hcdoc.Conflict("moved")
        self.docs[name] = (p["value"], ver + 1)
        return {"version": ver + 1}


JOB = {"schema": 1, "state": "pending", "owner": None, "attempt": 0, "max_attempts": 2, "result": {"out": None}}


class Validation(unittest.TestCase):
    def test_good_names(self):
        for n in ("sk-e051", "sk-e051-j007", "sk-" + "x" * 29, "sk-a_b"):
            self.assertEqual(hcdoc.check_name(n), n)

    def test_bad_names(self):
        for n in ("herdr-app", "herdr-app-x", "", "SK-A", "sk-a.b", "sk-a/b", "sk a", "sk-" + "x" * 30,
                  "t7", "sk-é", "other-1", "_sk-a", "-sk-a"):
            with self.assertRaises(hcdoc.BadInput, msg=n):
                hcdoc.check_name(n)

    def test_prefix_may_not_cover_the_app_doc(self):
        old = hcdoc.PREFIX
        try:
            for bad in ("herdr-", "h", "herdr-app", "Bad", ""):
                hcdoc.PREFIX = bad
                with self.assertRaises(hcdoc.BadInput, msg=bad):
                    hcdoc.check_name("herdr-app")
        finally:
            hcdoc.PREFIX = old

    def test_values(self):
        hcdoc.check_value({"schema": 1})
        for bad in (None, [], "x", 1, {}, {"schema": 2}, {"schema": "1"}, {"schema": True}, {"schema": 1.0},
                    {"schema": 1, "x": float("nan")}, {"schema": 1, "x": "a" * 1_000_000}):
            with self.assertRaises(hcdoc.BadInput, msg=str(bad)[:30]):
                hcdoc.check_value(bad)

    def test_size_limit_edge(self):
        base = len(json.dumps({"schema": 1, "x": ""}, separators=(",", ":")))
        hcdoc.check_value({"schema": 1, "x": "a" * (hcdoc.MAX_BYTES - base)})
        with self.assertRaises(hcdoc.BadInput):
            hcdoc.check_value({"schema": 1, "x": "a" * (hcdoc.MAX_BYTES - base + 1)})

    def test_multibyte_counts_as_bytes(self):
        with self.assertRaises(hcdoc.BadInput):
            hcdoc.check_value({"schema": 1, "x": "é" * 500_000})  # 1,000,000+ bytes in UTF-8

    def test_doc_set_refuses_before_calling_core(self):
        c = FakeConn()
        for name, value in (("herdr-app", {"schema": 1}), ("sk-a", {"x": 1}), ("nope", {"schema": 1})):
            with self.assertRaises(hcdoc.BadInput):
                hcdoc.doc_set(c, name, value, 0)
        self.assertEqual(c.sets, 0)

    def test_doc_get_refuses_app_doc(self):
        with self.assertRaises(hcdoc.BadInput):
            hcdoc.doc_get(FakeConn(), "herdr-app")


class Cas(unittest.TestCase):
    def setUp(self):
        self.sleeps = []
        self._old = hcdoc.sleep
        hcdoc.sleep = self.sleeps.append
        self.addCleanup(setattr, hcdoc, "sleep", self._old)

    def test_retries_then_succeeds(self):
        c = FakeConn({"sk-a": JOB}, rivals=2)
        ver, val = hcdoc.cas_update(c, "sk-a", lambda v: hcdoc.apply_ops(v, [("incr", "attempt", None)]))
        self.assertEqual(val["attempt"], 1)
        self.assertEqual(ver, c.docs["sk-a"][1])
        self.assertEqual(len(self.sleeps), 2)
        self.assertTrue(all(0.05 <= s <= 0.3 for s in self.sleeps), self.sleeps)

    def test_reapplies_on_fresh_value(self):
        c = FakeConn({"sk-a": dict(JOB, n=0)})
        seen = []

        def mutate(v):
            seen.append(v["n"])
            if len(seen) == 1:  # a rival increments after we read
                c.docs["sk-a"] = (dict(JOB, n=10), 2)
            v["n"] += 1
            return v

        _, val = hcdoc.cas_update(c, "sk-a", mutate)
        self.assertEqual(seen, [0, 10])
        self.assertEqual(val["n"], 11)

    def test_gives_up_loudly_after_five_retries(self):
        c = FakeConn({"sk-a": JOB}, rivals=99)
        with self.assertRaises(hcdoc.Lost) as cm:
            hcdoc.cas_update(c, "sk-a", lambda v: v)
        self.assertEqual(c.sets, 6)
        self.assertEqual(len(self.sleeps), 5)
        self.assertIn("5 retries", str(cm.exception))

    def test_missing_doc_is_bad_input(self):
        with self.assertRaises(hcdoc.BadInput):
            hcdoc.cas_update(FakeConn(), "sk-a", lambda v: v)

    def test_lost_from_mutate_is_not_retried(self):
        c = FakeConn({"sk-a": dict(JOB, state="running")})
        with self.assertRaises(hcdoc.Lost):
            hcdoc.cas_update(c, "sk-a", hcdoc.claim_mutation("t1"))
        self.assertEqual((c.sets, self.sleeps), (0, []))

    def test_cannot_drop_schema(self):
        c = FakeConn({"sk-a": JOB})
        with self.assertRaises(hcdoc.BadInput):
            hcdoc.cas_update(c, "sk-a", lambda v: hcdoc.apply_ops(v, [("set", "schema", 2)]))


class Mutations(unittest.TestCase):
    def test_claim(self):
        v = hcdoc.claim_mutation("t7")(dict(JOB))
        self.assertEqual((v["state"], v["owner"], v["attempt"], v["finished_ms"]), ("running", "t7", 1, None))
        self.assertIsInstance(v["started_ms"], int)

    def test_claim_refuses_non_pending_and_exhausted(self):
        for bad in (dict(JOB, state="running"), dict(JOB, state="done"), dict(JOB, attempt=2),
                    dict(JOB, attempt="1"), dict(JOB, state=None)):
            with self.assertRaises(hcdoc.Lost, msg=str(bad)):
                hcdoc.claim_mutation("t7")(bad)

    def test_claim_retried_job_increments(self):
        v = hcdoc.claim_mutation("t9")(dict(JOB, attempt=1))
        self.assertEqual(v["attempt"], 2)

    def test_if_checked_before_any_set(self):
        v = dict(JOB, owner="t1")
        with self.assertRaises(hcdoc.Lost):
            hcdoc.apply_ops(v, [("set", "state", "done"), ("if", "owner", "t2")])
        self.assertEqual(v["state"], "pending")

    def test_paths(self):
        v = hcdoc.apply_ops({"schema": 1}, [("set", "a.b.c", 5), ("incr", "a.b.c", None), ("incr", "n", None)])
        self.assertEqual(v, {"schema": 1, "a": {"b": {"c": 6}}, "n": 1})
        self.assertIsNone(hcdoc.path_get(v, "a.x.y"))
        for bad in ("a..b", "", "a b", "a.", "../x"):
            with self.assertRaises(hcdoc.BadInput, msg=bad):
                hcdoc.split_path(bad)
        with self.assertRaises(hcdoc.BadInput):
            hcdoc.apply_ops({"schema": 1, "a": 1}, [("set", "a.b", 1)])
        with self.assertRaises(hcdoc.BadInput):
            hcdoc.apply_ops({"schema": 1, "a": "x"}, [("incr", "a", None)])

    def test_parse_assign(self):
        self.assertEqual(hcdoc.parse_assign('a.b="x"', False), ("a.b", "x"))
        self.assertEqual(hcdoc.parse_assign("a=12", False), ("a", 12))
        self.assertEqual(hcdoc.parse_assign("a=12", True), ("a", "12"))
        self.assertEqual(hcdoc.parse_assign("a=x=y", True), ("a", "x=y"))
        for bad in ("novalue", "a=notjson", "bad path=1"):
            with self.assertRaises(hcdoc.BadInput, msg=bad):
                hcdoc.parse_assign(bad, False)


class Cli(unittest.TestCase):
    def run_hcdoc(self, *args, env=None, stdin=None):
        return subprocess.run([sys.executable, "-I", str(HCDOC), *args], capture_output=True, text=True,
                              env={**os.environ, **(env or {})}, input=stdin, timeout=30)

    def test_bad_name_exits_2_without_a_core(self):
        # HCDOC_CONNECT points nowhere: a bad name must still be rejected as bad input (2), not 4
        env = {"HCDOC_CONNECT": "/nonexistent", "HCDOC_TRANSPORT": "connect"}
        for name in ("herdr-app", "Bad", "other-1"):
            p = self.run_hcdoc("get", name, env=env)
            self.assertEqual(p.returncode, 2, (name, p.stderr))
        self.assertEqual(self.run_hcdoc("get", "sk-ok", env=env).returncode, 4)

    def test_feature_missing_exits_4(self):
        with tempfile.TemporaryDirectory() as d:
            core = Path(d) / "core.py"
            core.write_text(
                "import sys, json\n"
                "for line in sys.stdin:\n"
                "    m = json.loads(line)\n"
                "    print(json.dumps({'jsonrpc':'2.0','id':m['id'],'result':{'methods':['hello','terminal.list']}}), flush=True)\n")
            env = {"HCDOC_CONNECT": f"{sys.executable} {core}"}
            p = self.run_hcdoc("get", "sk-a", env=env)
            self.assertEqual(p.returncode, 4)
            self.assertIn("doc methods", json.loads(p.stderr)["error"])

    def test_core_exits_immediately_exits_4(self):
        env = {"HCDOC_CONNECT": "true", "HCDOC_TRANSPORT": "connect"}
        self.assertEqual(self.run_hcdoc("get", "sk-a", env=env).returncode, 4)

    def test_argparse_errors_are_2(self):
        self.assertEqual(self.run_hcdoc("set", "sk-a", "-").returncode, 2)  # --version missing
        self.assertEqual(self.run_hcdoc("update", "sk-a", "--set", "x").returncode, 2)


if __name__ == "__main__":
    unittest.main()
