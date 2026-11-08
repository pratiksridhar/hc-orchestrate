# Changes

Everything below the "Board mode" section is relative to the original skill (the initial commit
of this repo, formerly shipped as a zip).

## Scope check (`--scope`) — live test on hc 0.1.18, Claude Code worker

A writer's scope used to be prose in PLAN.md and its brief; `merge --dry-run` listed the files and
the lead had to spot strays. Now it is data that `collect` and `merge` check.

### Added
- `spawn --scope PATTERN...` (repeatable): the files a writer may change, relative to its repo root.
  Like `.gitignore` but always anchored: `*`/`?` stay in one folder, `**` spans folders, `[...]` is a
  class, a folder covers everything under it, and a literal path always matches itself (so
  `app/[id]/page.tsx` needs no escaping). Stored in `run.json`, listed in the brief's rules with
  "report `BLOCKED: needs <path> (outside my scope)`". Rejected for review/research roles; absolute
  paths, `..` and empty patterns are refused before anything starts. A writer without `--scope` gets
  a spawn warning and is not checked (old behavior).
- `collect`: `scope ok (...)` or `!! <name> changed files outside its scope (...): <files>`.
- `merge`: refuses (exit 1) before committing or merging anything when any pending worker has a file
  outside its scope; `--dry-run` marks those files `!!`; `--ignore-scope` merges anyway and says so.
- `hco scope --run R --name N [--add PATTERN...]`: show or widen one worker's scope, and print the
  `hc send` go-ahead for it.
- Workers in a shared local checkout (no `--worktree`) now get the same file-level snapshot as
  remote ones, so `collect` lists their files and checks scope (it used to say only "CHANGED").

### Found and fixed on the way
- Renames hid the old path: git's rename detection listed only `src/new.py` for a move out of
  `legacy/old.py`, so a deletion outside the scope went unseen. File lists now use
  `--no-renames` (worktree diff, worktree status and the checkout helper).
- Non-ASCII names came back C-quoted (`"docs/na\303\257ve.md"`) and never matched; lists now
  use `-z`.
- The checkout helper (`CHECKOUT_PY`, formerly `REMOTE_PY`) ran git in `--cwd` but joined paths to
  it, which broke for a `--cwd` below the repo top; it now works from the top, and skips any
  `.hc-orchestrate/` folder (an outbox in a subfolder was listed as a change).
- Old `run.json` files (`remote_git`, `fingerprint`) still collect.

### Verified live
Throwaway repo, one Claude worker (`hco-claude` template, worktree, `--trust`, `--scope 'src/**'`), task:
add `mul` to `src/calc.py` and a test in `tests/test_calc.py`.
- The brief rendered the scope; the worker did the in-scope part, left `tests/` alone and reported
  `BLOCKED: needs tests/test_calc.py (outside my scope)`; `collect`: `scope ok (src/**)`.
- `hco scope --add tests/test_calc.py` + the printed `hc send`: the worker finished, `DONE`;
  both tests passed in an hc terminal.
- A stray `notes.tmp` planted in the worktree: dry run marked it, `merge` refused with exit 1 and HEAD
  unchanged; after removing it `merge` committed and merged only the two scoped files.
- Not live-tested: Codex workers and `--on` workers with `--scope` (covered by the fake-hc tests).

### Tests
19 new in `test_hco.py` (`Scope`, `Units`, `Remote`), 58 there in total. Mutation-checked: putting
rename detection back fails the rename test; disabling the refusal fails the merge test.

## Remote workers (`--on`) live test (hc 0.1.18, omarchy: Linux, Claude 2.1.319, Codex 0.167.0)

### Verified
- Spawn writes the brief with `hc file write --on`; Claude and Codex start by direct command; the
  env scrub runs there. Both reported with `hc send` to the lead (`<hc from="omarchy/t20">`), so the
  brief's lookup of the lead through `parent` works across machines; `hco wait` woke on each.
- `status`/`collect` read the remote STATUS and RESULT.md; `cleanup` killed and removed the remote
  terminals.
- A new remote folder shows Codex's trust prompt (it may not for a folder trusted before);
  `--trust` can't help remotely, the user accepted it with `hc attach`.

### Found and fixed
- `collect` had no change check for remote workers (no worktree, no fingerprint), so a remote
  reviewer changing files went unnoticed. `spawn --on` now runs a helper there over `hc exec`
  (`REMOTE_PY`, needs `python3`) that snapshots HEAD plus a hash of every uncommitted file;
  `collect` lists files whose content differs since spawn and flags read-only roles. Files that
  were already changed before the worker started, and are unchanged by it, are not listed.
- Remote outboxes were not git-excluded and were left behind after `cleanup`. Spawn now adds the
  outbox and check artifacts to the remote repo's `.git/info/exclude`; `cleanup` fetches the outbox
  in one call and deletes it there only after that succeeded (and its empty parent folders).
- Tests: the fake hc now implements `exec` (base64 JSON like the real one) and `file read|write`;
  7 new tests in `Remote`.

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
