# hc doc store: verified assumptions (A1–A12)

Checked 2026-11-05 on `hc 0.1.17`, in an isolated session (`HC_SESSION=dst`,
scratch `XDG_STATE_HOME`/`XDG_CONFIG_HOME`; the socket path must be < 103 bytes,
so use a short `/tmp/dst.XXXX`). Raw JSON-RPC over `hc connect`; no mainframe
contact, no LLM calls.

## Existing skill (baseline, unchanged by the board)

The repo root: `SKILL.md` (lead playbook), `scripts/hco.py`
(stdlib CLI: preflight/init/spawn/status/wait/collect/merge/cleanup),
`references/`, `templates/`, `tests/` (`unittest` + `fake_hc.py`). State is on
disk in `.hc-orchestrate/<run>/` (PLAN.md, per-worker outbox STATUS/RESULT.md);
workers are LLM agents, terminals are labeled `hco.run=<run>`. The board is a
separate mode for deterministic `job_cmd` batches and touches none of this.

## Results

| # | Verdict | Evidence |
|---|---|---|
| A1 | confirmed | never-written `doc.get` → `{"value":null,"version":0}` |
| A2 | confirmed | `doc.set` v0 → `{"version":1}`; stale v0 → error `-32000`, `data.kind:"conflict"`, message `document "sk-t1" is at version 1, not 0` |
| A3 | confirmed | null write at v1 → `{"version":2}`; `doc.get` → `{"value":null,"version":2}`; stale `set` at v1 → conflict |
| A4 | **corrected** | rule is `1-32 of a-z 0-9 - _ .` (dot allowed), must start with a letter/digit, must not look like a terminal id (`t7`). `UPPER`, `a/b`, `a b`, empty, 33 chars, `-a`, `_a` → `invalid_params`. `herdr-app-x` is a legal name, so the helper must block `herdr-app` itself. The helper stays stricter (`^[a-z0-9_-]{1,32}$` + `sk-` prefix). |
| A5 | confirmed | limit is 1 MiB (1,048,576 B) of JSON: 1.1 MB → `invalid_params` "document exceeds 1 MiB of JSON"; 1.04 MB accepted |
| A6 | confirmed | `hello.methods` has `doc.get`, `doc.set` only; features: framed, attach, events, status, shell, resize_notifications |
| A7 | confirmed | `hc connect` is one JSON message per line; exits 0 when stdin closes |
| A8 | confirmed | unfiltered subscribe got `doc`; `labels={"exp":"x"}` got created/foreground/finished but **no** `doc` events |
| A9 | confirmed | subscribe reply is `{channel, seq, terminals}`, no docs. `missed` forced (see below): the fresh snapshot has terminals only, so docs must be re-read |
| A10 | confirmed | `echo $HC_TERMINAL_ID` in `hc run` printed `t1`, equal to the run's `address` |
| A11 | **corrected** | `hc kill` → `finished` (`reason:"killed"`, `signal:1`); `kill -9` of the program → `finished` (`reason:"exited"`, `signal:9`, `exit_code:null`), event carries `terminal:"t4"`. **`lost` did not fire** for a dead program; it is a holder/core-loss case. Orchestrator must treat `finished` as the death signal and handle `lost` too. A normal exit also emits `finished` (`exit_code:0`), so check job state before requeueing. |
| A12 | confirmed | `hc events --json` prints a snapshot line then doc events as `{"method":"event","params":{"type":"doc","data":{"name","version"},"seq":N}}`. Terminal events carry `params.terminal` |

## Extra findings that affect the design

- `doc.get`/`doc.set` accept optional `on` (remote machine) and can return
  `unavailable`, `needs_update`, `machine_unknown`, `internal`: map
  `unavailable`/`needs_update` to exit 4.
- Terminal addresses are never reused, including across `hc server stop` + restart:
  with finished terminals kept, the next was `t4` after `t1..t3`; after removing every
  terminal and restarting again, the next were `t5`, `t6` (counter persists). Owner =
  `$HC_TERMINAL_ID` is safe for orphan detection.
- **`missed` verified.** A subscriber stopped with SIGSTOP while 3000 doc writes happened
  buffered only ~78 events, then on resume received `{"method":"missed","params":{"seq":N,
  "terminals":[...]}}` (fresh terminal snapshot, **no docs**); the other ~2900 doc events
  were dropped. The `missed` notice was **not immediate**: in a first run (30000 writes,
  4 s drain window) it had not arrived yet, in the second it was present within 20 s. So
  a lagging watcher can act on stale state for up to a heartbeat interval (15 s): the
  orchestrator should also reconcile on a timer (e.g. every ~30 s) and not rely on
  `missed` alone. After `missed`: re-read manifest and every job doc.
- A doc write takes about 6–8 ms (atomic persist), so ~140 writes/s from one connection.
- Doc events are global and carry no value: always re-read after one.
- `hc ls --all --json` lists finished terminals with `exit_code`, usable at
  reconcile time to tell dead owners from live ones.
- Delivery is at-least-once: a worker killed between finishing `job_cmd` and
  writing its result reruns the job. `job_cmd` must be idempotent.
- Zombie guard: worker result writes must CAS on `owner == me and attempt == mine`.

## Design decisions taken

- The worker retries its own failures (`failed` → `pending` while
  `attempt < max_attempts`); the orchestrator only requeues crashed owners.

## Board implementation notes (hcboard.py, docstore_worker.py)

- **Files:** `scripts/hcdoc.py` (guarded doc access), `scripts/docstore_worker.py` (claims and runs jobs),
  `scripts/hcboard.py` (init / run / status / pause / resume). Tests: `tests/test_hcdoc*.py`,
  `tests/test_hcboard*.py` (`*_live.py` need `hc`; they use their own `HC_SESSION`), job stand-in
  `tests/fake_job.sh`.
- **Labels:** board terminals carry `exp=<id>`, `role=worker` and `hco.board=<id>`. `hco.run` is not used:
  it names a run folder under `.hc-orchestrate/` that the board does not create, so `hco status|cleanup`
  would fail on it. The orchestrator queries and removes only `hco.board=<id>` terminals.
- **Event flow:** the orchestrator runs `hcdoc watch --exit-with-parent` as a child (unfiltered subscription,
  client-side prefix and label filter), waits for its snapshot line (subscription live), then reconciles.
  Reconcile runs on: startup, any event on the manifest or its job docs, a worker `finished`/`lost`,
  `missed`, a restarted watcher, and every `--poll` seconds (default 30), debounced to one per 0.4 s.
- **Retry ownership:** the worker retries its own failures. The orchestrator only requeues jobs whose owner
  terminal is no longer live (`hc ls --all --label hco.board=<id>`), guarded by `state/owner/attempt`.
- **Safety nets:** a pending job with `attempt >= max_attempts` is failed by the orchestrator; ten worker
  crashes with no job finishing in between abort the run (no restart storm); the worker kills its job's
  process group on SIGHUP/SIGTERM; `zowe` anywhere in `job_cmd` is refused at `init` and in the worker.
- **Finish order:** manifest `done` -> export -> verify complete -> `git commit` -> `archived` -> delete job
  docs, then manifest -> `hc rm` finished worker terminals. A failed commit stops before any deletion;
  rerunning `run <exp>` resumes (`archived` + export present: only the deletions are redone).
- **Reads (added later, fake-core tests only):** the worker remembers jobs it saw `done` or `failed` and skips
  them when it looks for the next one; before, each claim re-read every finished job (1+2+...+n reads for n
  jobs). The orchestrator keeps the last value of every job doc and, on a doc event, re-reads only the docs
  named in events; startup, `missed`, a restarted watcher and the `--poll` timer re-read all, and "every job
  is final" is confirmed with a full read before finalizing. Measured on the fake core, 30 jobs and one
  worker: 806 doc reads before, 372 after (about 150 of those are `init`, export and deletion).
- **Retry delay:** `retry_delay_s` in the manifest. The worker's failure write sets `not_before_ms` =
  now + delay x 2^(attempt-1), capped at 600 s; `claim_next` skips a pending job until then and reports the
  shortest wait, and a worker with only waiting jobs sleeps (at most 5 s at a time, so it still sees a pause
  or cancel) instead of exiting. The guard is in the worker, not in `hcdoc claim`: a manual claim ignores it.
  A requeue after a worker crash sets no delay.
- **Cancel:** `hcboard cancel` sets the manifest to `cancelled` with `cancelled: true` (from planned, running
  or paused). On its next reconcile the orchestrator runs `hc kill` on its live labeled workers (their SIGHUP
  handler kills the job's process group), fails every job that is not final with error `cancelled` (a job
  that finished meanwhile is left alone), then finalizes as usual. With no orchestrator running, the next
  `run` does the same.
- **Retry failed:** `init NEW --retry-failed OLD` reads `experiments/OLD.json` (the docs are gone by then),
  takes the failed jobs' (case, variant) pairs and OLD's `job_cmd`, `pre`, `max_workers`, `max_attempts`,
  `job_timeout_s`, `retry_delay_s` (or a `--spec`), and records `parent: OLD`. `out_dir` is not copied: the
  new experiment writes to `runs/NEW/`. A spec may list `pairs` directly for a grid with holes.
- **Start-up checks:** before anything is started `run` verifies that `--cwd` exists, that `job_cmd[0]` is an
  executable (unless it contains a placeholder) and, unless `--no-commit`, that `--cwd` is inside a git work
  tree, that `experiments/<exp>.json` is not ignored and that git has a committer identity. Each of these
  used to surface only after every job had run, at the commit, or as every job failing `max_attempts` times.
- **`hc ls` row shape:** `references/hc-facts.md` records slim `hc ls --json` rows (no `name`, `labels`,
  `exit_code`), while this board's live test read `name` from the same command. Whichever holds on a given
  hc, `workers()` now completes a row that lacks `name` (or a finished row that lacks `exit_code`) with
  `hc get`. With slim rows the old code named every later worker `<exp>-w1`, so replacing a dead worker
  failed while w1 was alive, and clean worker exits counted towards the crash limit.
- **Not done:** OSC 7501 status from workers. (herdr.dev/core now shows the format,
  `printf '\e]7501;state=blocked:app=my-agent\e\\'`, but how `hc wait` on the orchestrator behaves once it
  reports a status needs a live core to check, so it is still not emitted.)
  Earlier note: `hc --skill` does not give the report format and the core
  documentation was not reachable here, so it was not guessed. Workers print progress lines instead
  (`hc read <worker>`).
- **Limits:** at-least-once delivery; one orchestrator per experiment (two would race on requeues);
  remote owners (`machine/t7`) are never requeued; jobs are re-read in full on every reconcile (fine for
  hundreds of jobs, ~7 ms per read).
