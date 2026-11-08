# Changes

Everything below the "Board mode" section is relative to the original skill (the initial commit
of this repo, formerly shipped as a zip).

## Merge live test (hc 0.1.18, Claude Code 2.1.319)

Throwaway repo; a Claude writer (`hco-claude` template) added a function, a Codex writer
(`hco-codex` template) added tests, each in its own worktree with `--trust`.

### Verified
- Template success path: both tasks went in (`first_message.sent: true`), no trust prompt.
- Both reports arrived as tagged `hc send` messages, including Codex's (bypass mode has no
  sandbox); `hco wait` woke on each.
- `merge` conflict path: aborted, root left clean, exit 1. `cleanup --worktrees` removed only
  merged worktrees and kept branches.

### Found and fixed
- `merge` committed check artifacts: running each worker's check left
  `__pycache__/calc.cpython-313.pyc` in both worktrees, `git add -A` committed it, and the
  second merge conflicted on it although the workers shared no source file. Once tracked, every
  later run of the code dirtied the root and blocked `merge`.
  - `init` now also adds common artifacts to `.git/info/exclude` (`__pycache__/`, `*.py[cod]`,
    `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`, `.coverage*`, `node_modules/`, `.DS_Store`),
    idempotently. Worktrees share that file, so `collect` and `merge` skip them.
  - New `merge --dry-run`: each worker's files and commits ahead, nothing changed.
  - Tests: artifact regression (fails on the old code), idempotent exclude, dry run.
- Docs: Codex's sandbox blocks `hc send` only in `--permissions safe`.

## Board mode (branch `hc-doc-get-set`)

An opt-in experiment/job board on herdr-core's doc store (`doc.get` / `doc.set`). Nothing in the
existing `hco` flow changed; if the doc methods are missing, `hcboard init` exits 4 and the lead
uses the normal flow.

### Added
- `scripts/hcdoc.py`: guarded doc access (`get set update claim delete watch export`). Names
  `^[a-z0-9_-]{1,32}$` with prefix `sk-`, never `herdr-app`, values are objects with `"schema": 1`
  under ~1,000,000 bytes, CAS retry (5 retries, 50-300 ms jitter), `update` with `--if/--set/--incr/--now`
  (no expression eval), transport `hc connect` with a Unix-socket fallback, exit codes 0/2/3/4.
- `scripts/docstore_worker.py`: claims the first pending job (CAS), runs `job_cmd` with placeholders,
  writes the result guarded by `owner/attempt/state` (a zombie cannot overwrite a newer attempt),
  retries its own failures until `max_attempts`, kills its job's process group on SIGHUP/SIGTERM.
- `scripts/hcboard.py`: `init`, `run`, `status`, `pause`, `resume`. `run` subscribes first, then
  reconciles (startup, doc events, worker exit, `missed`, watcher loss, every 30 s), requeues orphans,
  tops up workers, and finishes with export, `git commit`, `archived`, doc deletion and worker cleanup.
- `SKILL.md`: a "Board mode" section; the Plan phase tells the lead to propose the board; ground rule 4
  also allows `hco.board=<exp>` terminals.
- `references/hc-docstore-notes.md`: A1-A12 verified on hc 0.1.17 plus design decisions.
- `hc-orchestrate-workflow.html`: reworked with a board-mode section (diagrams, data model, job life,
  orchestrator loop, run walkthrough, verified facts, tests, quiz). Fixed a duplicate `#quiz` id that
  rendered the quiz inside its heading.
- `.gitignore` for `__pycache__/`; the tracked `.pyc` files were removed; the stale zip was removed.

### Verified against hc 0.1.17 (isolated session)
- Confirmed: `doc.get` null/version 0, stale `set` -> `conflict`, null keeps the version, 1 MiB limit,
  no `doc.list`, `hc connect` JSON lines (and plain JSON on the socket), label filters drop doc events,
  snapshots hold no docs, `$HC_TERMINAL_ID` is the terminal address, `hc events --json` includes doc events.
- **Corrected:** doc names also allow `.` and must not look like a terminal id (the helper stays stricter);
  a dead worker gives `finished` (`hc kill`: signal 1, `kill -9`: signal 9), `lost` did not fire.
- Forced `missed`: a stalled subscriber buffered ~78 events, then got `missed` plus a terminal snapshot
  (late, up to a heartbeat), hence the 30 s reconcile timer. Terminal addresses are never reused, even
  across a core restart.

### Tests
84 pass: the original 21, plus `test_hcdoc.py` (24) and `test_hcboard.py` (22) offline, and
`test_hcdoc_live.py` (9) and `test_hcboard_live.py` (8) against a real core. The live tests set their own
`HC_SESSION` and scratch XDG dirs, use `tests/fake_job.sh` (no LLM, no mainframe), stop only that core, and
skip without `hc` (or with `HCDOC_NO_LIVE=1`). Run all: `python3 -I -m unittest discover -s tests -v`
(about 90 s). Live scenarios: validation, delete-keeps-version, claim race, full run (12 jobs, 3 workers),
crash recovery, max attempts, pause/resume, orchestrator restart, watcher loss, lineage, init refusals.

### Not done / open
- Worker OSC 7501 status (format not in `hc --skill`; not guessed).
- One orchestrator per experiment; remote owners are never requeued.


## Fixed
- `hco.py` `terminals_for`: real `hc ls --json` returns slim rows (no `name`, `labels`,
  `reported`, `exit_code`; `status` is a bare string). Records now come from `hc get --json`
  per terminal. Before, every worker showed as `gone` and `status` would crash.
  (Found by running against hc 0.1.17; covered by `test_status_sees_worker_via_get_not_slim_ls`.)
- Worker brief: tells workers to write `RESULT.md`/`STATUS` with their file tool (not shell
  heredocs) and to run `hc send` as a single unchained command. In the live test the worker
  chained `cat > ... && hc send ...` in one shell call, which missed the `Bash(hc send *)`
  allow rule and blocked on a permission prompt.

## Changed on request
- **Bypass is now the default.** Templates and the direct commands use
  `--dangerously-skip-permissions` (Claude) and `--dangerously-bypass-approvals-and-sandbox`
  (Codex). `hco spawn --permissions safe` restores the original acceptEdits / workspace-write
  behavior (direct command, no template). Preflight warns; `run.json` and spawn output record it.
- **Env scrub.** Finding: workers get the hc core's environment, not the lead's shell (24 vars here,
  only `SSH_AUTH_SOCK` secret-looking), so the exposure is smaller than assumed; files on disk
  (`~/.broadcom-creds`, `~/.ssh`) are the larger exposure and are NOT covered. Workers now start
  through a one-line `sh` snippet that unsets secret-looking vars (kept: ANTHROPIC_*, OPENAI_*,
  CODEX_*, CLAUDE*, HC_*, HERDR_*) then `exec`s the agent. In templates and direct commands;
  `--no-scrub` opts out. Verified in a real hc terminal and for template round-trip; 3 tests.

## Trimmed
- `SKILL.md` 1,601 -> ~1,200 words: same phases and rules, tables and repeated explanations
  condensed; added the trust-dialog and Codex-reporting caveats from the live runs.
- `references/hc-facts.md` and README corrected (slim `ls` rows; Codex `hc send` blocked).

## Added
- `hco merge --run R [--name N]...`: commits each worktree worker's changes (workers never commit, so
  their branches were empty) and merges `hco/<run>/<name>` with `--no-ff`, one at a time. Refuses a
  dirty root; aborts and exits 1 on conflict, leaving the root clean; rerun is a no-op. Verified on
  real git (the `efi1` scratch repo: commit, merge, rerun, worktree cleanup) plus 4 unit tests.
- `tests/fake_hc.py` (stand-in hc with real-hc JSON shapes) and `tests/test_hco.py`.
  Run: `python3 -I -m unittest discover -s tests -v`. `HCO_SCRIPT=<path>` tests another copy.

- Wait table: a worker that never reported with turn 0 now hints at a folder-trust/login dialog.

## Live-verified (hc 0.1.17, Claude Code 2.1.318, Codex with GPT-6.1-Sol)
- preflight, init, spawn (direct launch), wait, status, collect, cleanup.
- Claude writer in `--worktree` (branch `hco/<run>/build`) -> Codex read-only reviewer pointed at
  that worktree with `--cwd`. Reviewer changed nothing; `collect` confirmed it.
- Claude's `hc send` report reaches the lead as `<hc from=...>`.

## Live findings
- A fresh worktree triggers Claude's folder-trust dialog. hc status stays empty (turn 0, never
  reported), so it looks idle. A person must accept it (`hc attach`); the lead must not.
- Codex's sandbox DOES block `hc send` ("hc could not start its core: Operation not permitted").
  The outbox files plus Codex's own OSC status (`done`) carried the report, so `hco wait` still
  worked. The brief's "don't retry" rule behaved correctly.
- Codex needed no trust prompt in the same worktree and used `hc --skill` on its own.

## Templates (tested in an isolated core)
`hc` ignores `XDG_CONFIG_HOME` for templates in the user's running core. To test without
installing anything, use a separate core: `HC_SESSION=<name> XDG_CONFIG_HOME=<scratch>` (hc's
own advice for tests); stop it with `HC_SESSION=<name> hc server stop`. Verified: both
`hco-claude` and `hco-codex` load, preflight reports them, `spawn` launches via
`template:hco-*`, and with a folder-trust dialog showing, `first_message.sent=false` comes
back with a clear reason and `spawn` exits 3. The accepted-trust (success) path was skipped.
Note: `hc send` to a trust dialog would press Enter on the default option (Claude: "No, exit"),
so recovery must be human-accepted first.

## Not yet verified (original skill)
Template success path (task pasted after readiness), Claude writer's permission prompt after the brief fix (no prompt seen
this run), `--on` remote.
