# Changes

Everything below the "Board mode" section is relative to the original skill (the initial commit
of this repo, formerly shipped as a zip).

## Notes board: workers share short facts — fake-core tests only, NOT live-tested

Opt-in with `hco init --notes`; a run without it behaves exactly as before and never touches the doc store.

### Added
- `scripts/hcnotes.py`: `read --run R [--since N]` and `post --run R --from NAME --kind KIND [--re N] TEXT`.
  Kinds `FINDING`, `INTERFACE`, `QUESTION`, `ANSWER`. One line of at most 500 characters (control characters
  removed), 20 posts an author, 200 a run. One doc per post, written with compare-and-set at version 0.
- `hco init --notes` opens the board (exit 4 and nothing created when the core has no doc store). The brief
  of every local bypass-mode worker gets a "Shared notes board" section: when to read, what to post, and that
  posts are unverified claims that never override scope or rules. `--on` and `--permissions safe` workers get
  no board and `spawn` warns.
- `hco notes --run R` (the lead's view; `--post`, `--re`, `--kind` to add its own), a `=== notes board`
  section at the end of `hco collect`, a `notes: n new post(s)` line under the `wait`/`status` table, one
  `note` entry per post in the journal, and `cleanup` archives the posts to `notes.json` and deletes the docs.
- Author check: a post records `$HC_TERMINAL_ID`; the lead's views flag a post whose author is not a worker
  of the run or whose terminal is not the one `hco` started for that worker.

### Tests and limits
- `tests/test_hcnotes.py`, 16 tests against `fake_core.py` + `fake_hc.py`: posting from four processes at
  once, limits, the brief, author flags, the journal, an unreachable store, cleanup and its rerun.
- Not tested: a real core, and real agents. Whether workers post useful things, read at the right moments
  or flood the board is unknown; a real run should count the posts that changed what another worker did.
- `--kind BLOCKED` from the first sketch was dropped: a blocked worker must tell the lead through STATUS,
  and a second place to say it would hide it. `ANSWER` was added so a `QUESTION` can be closed.
- `hc-orchestrate-workflow.html` does not show the notes board yet.

## Board mode: fail early, cancel, retry only what failed — fake-core tests only, NOT live-tested

### Found and fixed
- **A run could do all its work and then fail at the last step.** The export is committed at the end; in a
  folder that is not a git repo, with `experiments/` ignored, or without a git identity, that commit failed
  after every job had run. A `job_cmd` that could not start failed every job `max_attempts` times and the run
  "completed". `run` now checks both before it starts anything and exits 2 with the reason.
- **Reads grew with the square of the job count.** Each claim made the worker re-read every job from the top,
  finished ones included, and each doc event made the orchestrator re-read all jobs. 30 jobs with one worker
  took 806 doc reads; now 372 (about 150 of them fixed cost), growing linearly. See
  `references/hc-docstore-notes.md` for how the cache is kept honest (timer, `missed`, a confirming full read).
- **Worker numbering depended on `hc ls` rows carrying `name`.** `references/hc-facts.md` says they don't;
  this board's live test read `name` from them. I could not check which is true (no hc here). With slim rows
  the old code named every later worker `<exp>-w1`, so replacing a dead worker failed while w1 lived
  (reproduced against the fake core in slim mode), and clean exits counted as crashes. Rows are now
  completed with `hc get` when fields are missing, which is right under either shape.

### Added
- `hcboard cancel EXP`: there was no way to stop an experiment except `pause` (running jobs continue, docs
  stay forever) or killing terminals by hand. Cancel kills the experiment's workers and their jobs, fails
  unfinished jobs as `cancelled`, exports and archives; the last line ends `CANCELLED`.
- `hcboard init NEW --retry-failed OLD [--spec S]`: finished experiments are archived and their docs deleted,
  so failed jobs could only be rerun by rerunning the whole grid. This builds a child experiment from the
  failed jobs in OLD's export. The final output of a run with failures prints the command.
- Spec `retry_delay_s`: a failed job waits (delay, doubling, at most 10 min) before its retry instead of
  burning all attempts within seconds; the worker stays alive meanwhile. Default off: behavior unchanged.
- Spec `pairs`: an explicit list of (case, variant) jobs instead of the full grid.
- `status`: time so far, mean time per finished job, a rough time left, retried attempts, totals per
  variant (`stats` and `variants` in `--json`).
- `tests/fake_core.py` and `tests/test_hcboard_offline.py`: the orchestrator, worker and `hcdoc` now run end
  to end without hc (a file-backed doc store with compare-and-set and events, plus `ls/get/run/kill/rm`
  that really start and signal processes). Before this, everything but pure functions was covered only by
  the live tests, which CI skips. 21 tests: full run, crash requeue, pause/resume, and everything above.

### Tests and limits
- 194 tests in total; 17 live ones skip without `hc`. Mutation-checked (each caught): start-up checks off,
  cancel without kill, cancel ignored, slim rows not completed, delay not honored, worker exiting instead
  of waiting, worker cache off, orchestrator cache off, no confirming full read, `missed` keeping the
  cache, retry taking every job.
- The fake core follows `references/hc-docstore-notes.md` (A1-A12) but is not the core. Not confirmed live:
  that `hc kill` on a worker reaches its SIGHUP handler exactly as the notes describe (cancel relies on it
  to stop the job's process group), and event timing under load.
- Not done: OSC 7501 status for the orchestrator and workers (the format is now on herdr.dev/core; its effect
  on `hc wait` needs a live check), requeue of remote owners, more than one orchestrator per experiment,
  the HTML guide.

## Run state, plans, checks and rounds — fake-hc tests only, NOT live-tested

The agent flow kept its coordination in the lead's head: dependencies were prose in PLAN.md, "verified"
was something the lead said, a follow-up was a bare `hc send`, and a restarted lead had no way back
into its run. This change turns each of those into data that `hco` checks. Nothing an existing run
does changes unless a new option is used; old `run.json` files still load.

### Found and fixed on the way
- **Parallel spawns lost workers.** `spawn` read `run.json`, started the terminal (seconds), then wrote
  its stale copy back. Six spawns started together left one worker in `run.json`; the other five
  terminals ran unrecorded, invisible to `status`, `collect`, `merge` and `cleanup` (reproduced with the
  previous `hco.py` and the new test: `['w4'] != ['w0', ..., 'w5']`). Agents that issue several tool calls
  at once hit this easily. Every write now happens under a file lock (`.lock`, `fcntl`) on a fresh read,
  and the temp file is per process.
- **A failed spawn blocked its own retry.** When `hc run` failed after `git worktree add`, the worktree
  and branch stayed, and spawning the same name again died on "a branch named ... already exists". A
  failed spawn now removes the worktree, branch and outbox it made. The name is reserved in `run.json`
  while a spawn runs (5 minutes at most); a worktree left by a spawn that was killed outright is reported,
  never deleted or reused.
- `spawn` crashed with a traceback when `hc run` gave no answer in 180 s, with the terminal possibly
  running unrecorded. It now looks the terminal up and records it with a warning, or rolls back.
- `status` and `collect` showed `(no STATUS file)` for every worker after `cleanup --worktrees` (the
  outbox lived in the removed worktree). They now fall back to the archived copy.
- The remote outbox archive copied only `STATUS` and `RESULT.md`; it now takes every file.

### Added
- **`hco plan` / `hco advance`.** A `plan.json` lists the workers with `spawn`'s options plus `after`
  (`name:done`, `name` = accepted, `name:merged`) and `"cwd": "@name"`. `plan` reports every problem at
  once and stores nothing on error: unknown keys, bad names, missing briefs, unknown or cyclic
  dependencies, `:merged` on a worker without a worktree, scopes on read-only roles, and writers that can
  run together whose scopes may share a file (`--allow-overlap` downgrades that to a warning). Briefs are
  copied into the run. `advance` starts what is ready up to `max_parallel` (default 4), appends an
  "Inputs from earlier workers" section with their RESULT.md paths, and prints JSON: `started`, `waiting`
  with the reason per worker, `errors`, `complete`. One failing spawn does not stop the others.
- **`spawn --check CMD`, `hco check`.** The command is stated in the brief ("report DONE only when it
  exits 0; never weaken a test to get there"). `check` runs it with `sh -c` in the worker's folder inside
  an hc terminal (`--no-notify`, label `hco.check=<name>`), output to `checks/<name>.log`, and records exit
  code, duration and the tree id of the content. Exit 0 / 1 / 124 (still running; the same command resumes
  the wait, no second terminal). A change to the folder while the check ran makes the result not count,
  and the changed files are listed.
- **`hco accept`.** Records the lead's verdict with a note and the tree id. Refuses a STATUS other than
  DONE, a check that has not passed on the current files, out-of-scope files and a read-only worker that
  changed files; `--force` stores the override and its reasons.
- **Merge gates.** `merge` refuses (exit 1, nothing committed) a worker whose check is missing, failed or
  older than its files, and a worker that changed after `accept`. `--dry-run` marks them; `--skip-checks`
  overrides and says so. A worker without a check and without an accept merges as before. Merges are
  recorded (`merged.commit`).
- **`hco send`.** A follow-up round: text to `FOLLOWUP.r<n>.md`, old `STATUS`/`RESULT.md` renamed to
  `STATUS.r<n-1>`/`RESULT.r<n-1>.md` (before this, a worker on round 2 still showed `DONE` from round 1),
  the earlier accept dropped, one-line `hc send` pointing at the file. Rounds are counted against
  `--max-rounds` (default 3; exit 4 beyond). If `hc send` fails the files are put back. Works for `--on`
  workers through the checkout helper.
- **`NEXT` column** in `status`/`wait` (also in `status --json`, with `round` and `age_min`): the
  supervise table from SKILL.md computed per worker. `spawn --deadline MIN` adds "over its N min budget".
- **`hco adopt`, polling `wait`, `hco runs`.** For a lead restarted in a new terminal: `runs` finds the
  run without hc; `adopt` records the new lead, rewrites the address in local workers' briefs and sends
  each live one a message. `wait` polls hc state and STATUS files when a live worker was started by
  another terminal, when hc answers `needs_target`, or with `--poll`; it stores what it last showed, so a
  change between two waits is not missed.
- **Journal.** `events.jsonl` gets one line per action (init, spawn, spawn_failed, plan, advance,
  check_started, check, accept, followup, scope_add, merge, merge_refused, merge_conflict, adopt,
  cleanup). `hco log` prints it; `hco report` writes `REPORT.md` (a table per worker and the timeline).
- `.github/workflows/tests.yml`: the unit tests on Python 3.11, 3.12 and 3.13.

### Design notes
- **Tree id, not timestamps.** A check or accept is tied to `git write-tree` of the folder's content,
  built in a throwaway index (`GIT_INDEX_FILE`), outboxes excluded by pathspec. Committing the same
  content gives the same id, which is why `merge` (which commits the worktree) can be rerun after a
  conflict without a new check. It hashes every file on each call: fine for normal repos, slow for huge ones.
- **`wait` decides from hco's own record** of which lead started each worker (`workers.<n>.lead`), not
  from hc's `parent` field, whose exact form for local children is not in `references/hc-facts.md`.
- Errors now travel as an exception to `main` instead of `sys.exit` inside helpers, so `advance` can
  report one worker's failure and go on. Messages and exit codes are unchanged.

### Assumptions about hc that the fake encodes and no live run confirmed
`hc` could not be installed where this was written. These are taken from `references/hc-facts.md` and
SKILL.md, and need one live pass:
- `hc wait <t> --json` on a plain program returns when it exits, and `hc get <t> --json` then shows
  `state: "finished"` with its `exit_code` (the check's verdict depends on it).
- `hc run --no-notify` keeps the check's exit note out of the lead's input. Its exit may still wake
  `hco wait` as child news; that is harmless (it shows as a `(name)` process row).
- `hc send <t> "<one line>"` exits non-zero when the text was not delivered (`hco send` then puts the
  report files back). If it can fail after delivering, the worker would have the message but find its
  old STATUS restored.
- `hc wait --children any` from a terminal without children fails with exit 7 / `needs_target`. If it
  waits silently instead, an adopted run still polls, because that is decided from `run.json`.
- A message sent to a worker mid-turn (`adopt`) is queued by the agent rather than disturbing its turn.

### Tests
`tests/test_hco_flow.py`: 52 new tests (`RunState`, `Check`, `Accept`, `FollowUp`, `NextStep`, `Restart`,
`Plan`, `Units`); 173 in total, 17 of them live tests that skip without `hc`. The fake hc gained `send`,
`wait <t>` (runs a check terminal's command), a request lock like the real core, and knobs for failing or
slow `run`, refused `send` and `needs_target`. Mutation-checked, each caught by a test: no rollback, merge
gate off, stale check counted as passed, accept ignoring the tree, no report rotation, no round limit,
no overlap check, dependencies ignored, `max_parallel` ignored, tree id built in the real index, polling
never chosen, a check counting although files moved under it.

### Not done
- `hco check` for `--on` workers; telling a remote worker its new lead in `adopt`.
- `hc-orchestrate-workflow.html` does not show the new commands yet.
- Board mode is untouched.

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
