# hc-orchestrate: technical specification

| | |
|---|---|
| Status | Describes the code at commit `7467625` (branch `main`, 2026-11) |
| Audience | Engineers familiar with multi-agent orchestration, terminal multiplexers and optimistic concurrency |
| Sources of truth | `scripts/*.py` (behavior), `SKILL.md` (the lead agent's playbook), `references/hc-facts.md` (assumed herdr-core behavior) |
| Companion docs | `README.md` (user guide), `hc-orchestrate-workflow.html` (visual walkthrough), `CHANGES.md` (history and test status) |

Where this document and the code disagree, the code wins. Statements about herdr-core (`hc`) are the
assumptions this repo encodes, checked against hc 0.1.17–0.1.20. They are not guarantees from herdr-core.

---

## 1. Summary

hc-orchestrate is a **skill**: a prompt (`SKILL.md`) plus a stdlib-Python toolkit (`scripts/`). It turns
one interactive coding agent (Claude Code or Codex), the **lead**, into a supervisor of other interactive
agents, the **workers**. Every agent runs in a persistent terminal owned by **herdr-core** (`hc`), a local
terminal server that tracks agent status, carries messages between terminals and stores small versioned
JSON documents.

The design has three ideas:

1. **The LLM decides, the toolkit enforces.** The lead plans, writes briefs and judges results. `hco.py`
   holds all state and every gate: run ids, worktrees, briefs, scope checks, content-addressed check and
   accept records, merges and cleanup. Rules that matter are checked in code, not left to the prompt.
2. **Worker output is a claim, never a fact.** Reports are verified (check command, diff against scope,
   lead review) before anything is merged, and a gate stops counting once the files change.
3. **Event-driven supervision on a durable substrate.** The lead blocks on `hc wait` (status events
   the agents report themselves over OSC 7501) instead of polling screens. Run state lives on disk and,
   for team and board mode, in hc's doc store, so a crashed or restarted lead can take a run over.

There are three operating modes that share the substrate:

| Mode | Workers | Coordination | State |
|---|---|---|---|
| **Run** (default) | LLM agents, one brief each | Lead ↔ worker only (star topology) | `.hc-orchestrate/<run>/run.json` + per-worker outbox files |
| **Team** (`hco init --team`) | LLM agents | Adds a shared task list (CAS claims) and mail between workers | Run state above + hc doc store (`sk-<run>-*`) |
| **Board** (`hcboard.py`) | Deterministic `job_cmd` processes, not agents | Orchestrator process + job-claiming workers | hc doc store only (`sk-<exp>*`); exported to git at the end |

---

## 2. System context

```mermaid
flowchart TB
    user(["User"]) -->|"/hc-orchestrate &lt;task&gt;"| lead
    subgraph core["herdr-core (hc) server, local socket"]
        direction TB
        terms["Terminals: PTY, status (OSC 7501),<br/>parent/child, labels, events"]
        docs[("Doc store<br/>versioned JSON, CAS on version")]
    end
    lead["Lead agent<br/>(Claude Code / Codex) in hc terminal"] -->|"python3 hco.py …"| hco["hco.py<br/>(run state, gates)"]
    hco -->|"hc run / get / ls / wait / send / kill / rm"| terms
    hco -->|"git worktree / diff / write-tree / merge"| git[("Git repo")]
    terms --> w1["Worker 1 (claude)"] & w2["Worker 2 (codex)"]
    w1 & w2 -->|"edit files"| wt[("Worktrees<br/>hco/&lt;run&gt;/&lt;name&gt;")]
    w1 & w2 -->|"RESULT.md + STATUS"| outbox[("Outbox files")]
    w1 & w2 -->|"hc send lead '[hco run/name] …'"| lead
    w1 & w2 -.->|"team mode: hcteam.py"| docs
    hco -.->|"team mode"| docs
    board["hcboard.py orchestrator<br/>+ docstore_worker.py"] -.->|"board mode"| docs
```

### 2.1 Components

| File | Lines | Role |
|---|---|---|
| `SKILL.md` | ~340 | The lead's playbook: ground rules and phases 0–6 (preflight, plan, spawn, supervise, verify, integrate, report/cleanup), plus team and board procedures. Loaded by Claude Code (`/hc-orchestrate`) or Codex (`$hc-orchestrate`). `disable-model-invocation: true`, so it runs only when invoked by name. |
| `scripts/hco.py` | ~2900 | The run-mode CLI and all gates. 20 subcommands (§11). |
| `scripts/hcteam.py` | ~610 | Team mode: task list, claims, mail. Called by workers directly and by `hco` as a library. |
| `scripts/hcdoc.py` | ~660 | Guarded JSON-RPC client for the doc store: name/size validation, CAS update with retry, claim, watch, export. |
| `scripts/hcboard.py` | ~840 | Board-mode orchestrator: `init`, `run` (event loop), `status`, `pause`/`resume`/`cancel`. |
| `scripts/docstore_worker.py` | ~255 | Board-mode worker: claim the next job, run `job_cmd`, write a guarded result, repeat. |
| `templates/hco-{claude,codex}.md` | 9 each | hc launch templates: env scrub + agent CLI in bypass mode, plus an opening instruction. |
| `install.sh` | 81 | Copies (or symlinks) the skill to `~/.claude/skills` and `~/.agents/skills`, and the templates to `~/.config/herdr-core/templates`. |
| `references/` | | `hc-facts.md` (hc behavior relied on), `hc-docstore-notes.md` (doc-store experiments A1–A12 and board design), `patterns.md` (recipes, brief guide). |
| `tests/` | ~4.7k | `unittest`; fakes (`fake_hc.py`, `fake_core.py`) plus `*_live.py` tests against a real hc and, opt-in, real Claude workers. |

All scripts need only Python 3.8+ and the standard library (CI runs 3.11–3.13). `fcntl` locks are skipped
on Windows.

---

## 3. Substrate: what herdr-core provides

The design depends on these hc behaviors (`references/hc-facts.md`):

| Primitive | Used for | Notes |
|---|---|---|
| `hc run --bg --name N --cwd D --label k=v -- <cmd\|template>` | Starting workers, checks, board workers | Names are 1–32 of `[a-z0-9_-]` and unique among live terminals. Addresses (`t7`, `machine/t7`) are never reused, even across server restarts. |
| Parent/child | Worker lifetime, event routing | A terminal started from inside another is its child and dies with it unless `--outlive-parent`. `hc wait --children` sees only the caller's own children. |
| **OSC 7501** status | Supervision | The program itself reports `idle / working / done / blocked(kind) / error`. hc never infers status from the screen. Claude Code ≥ 2.1.295 and current Codex report it. |
| Templates | Launch handshake | hc waits for the agent's first OSC 7501 report (up to 60 s on 0.1.20), then pastes `body + task` and returns `first_message.sent`. |
| `hc wait [--children any] --timeout S` | Lead's blocking loop | Returns on a turn ending, blocked, error, exit, or an incoming message. A timeout is a normal check-in. |
| `hc send <t> <text>` | Lead → worker steering, worker → lead reports | Tagged `<hc from="t7 …">` only if both sides have reported OSC 7501. Hence the explicit `[hco <run>/<name>]` prefix. |
| `hc get` / `hc ls` | State | `ls --json` returns slim rows (no `name`, `labels`, `exit_code`), so `hco` lists by label and then `get`s each terminal (`terminals_for`, `hco.py:750`). |
| `hc exec/file --on M` | Remote workers | Base64 stdout/stderr in `exec --json`; exit 0 even on failure, so read `code`. |
| Doc store: `doc.get`, `doc.set(name, value, version)` | Team and board state | `get` of an unwritten doc → `{value:null, version:0}`. `set` with a stale version → `conflict`. Null write = delete (version kept, so a name is never "fresh" again). 1 MiB per doc, ~6–8 ms per write. **No list call, unencrypted, events carry no value.** |
| Event subscription | Board orchestrator | Doc and terminal events as JSON lines. A lagging subscriber gets a `missed` notice with a terminal snapshot and no docs, and the notice can arrive late. |

---

## 4. Run mode

### 4.1 Lifecycle

```mermaid
flowchart LR
    P0["0 preflight"] --> P1["1 init + plan"] --> P2["2 spawn / advance"] --> P3["3 wait (supervise)"]
    P3 -->|STATUS DONE| P4["4 collect · check · accept"]
    P4 -->|"defect: hco send (round n+1)"| P3
    P4 --> P5["5 merge"] --> P6["6 report · cleanup"]
```

| Phase | Commands | Notes |
|---|---|---|
| 0 Preflight | `hco preflight` | BLOCKs unless `$HC_TERMINAL_ID` names a live terminal, and unless a Codex lead runs with `--no-daemon` (without it Codex's shell commands run outside hc). WARNs on a missing template, old Claude, no git. |
| 1 Plan | `hco init --task T [--team]`, `hco plan --file plan.json` | `init` makes a 4-character run id (first character a letter), the run folder, and `.git/info/exclude` entries. A stored plan is optional and is advised for ≥ 3 workers or any dependency. |
| 2 Start | `hco spawn …` or `hco advance` | See §4.3 and §4.6. |
| 3 Supervise | `hco wait --timeout 100` (repeat) | Wraps `hc wait --children any` and prints a status table with a `NEXT` column (§4.5). 100 s stays under Claude Code's 120 s Bash tool limit. |
| 4 Verify | `hco collect`, `hco check`, `hco accept`, `hco send`, `hco scope --add` | §4.7 |
| 5 Integrate | `hco merge [--dry-run] [--name N]…` | §4.8 |
| 6 Close | `hco report`, `hco cleanup [--worktrees]` | Cleanup archives outboxes, kills and removes only terminals labeled `hco.run=<run>`, closes the team, and optionally removes clean merged worktrees. Branches are kept. |

### 4.2 Persistent state

`run.json` is the single source of truth for a run. Every write goes through `update_run`
(`hco.py:266`): take an exclusive `flock` on `<rundir>/.lock`, read fresh, mutate, then write a temp file
and `os.replace` it. Parallel `hco spawn` calls are therefore safe. Every action also appends one line to
`events.jsonl` (the journal behind `hco log` and `hco report`).

```jsonc
{
  "run": "k3x9", "task": "…", "root": "/repo", "lead": "t3", "created": "…",
  "former_leads": [{"lead": "t1", "until": "…"}],           // after `adopt`
  "starting": {"api": 1731500000.0},                         // name reservations, 300 s TTL
  "team": {"doc": "sk-k3x9-team", "closed": null, …},        // init --team
  "plan": {"max_parallel": 4, "workers": [ /* validated entries */ ]},
  "workers": {
    "api": {
      "terminal": "k3x9-api", "address": "t7", "agent": "claude", "role": "implement",
      "launch": "template:hco-claude" | "direct", "permissions": "bypass" | "safe", "scrub": true,
      "cwd": "…/wt/api", "outbox": "…/.hc-orchestrate/k3x9/out/api", "on": null,
      "worktree": "…/wt/api", "branch": "hco/k3x9/api", "base": "<sha>",
      "scope": ["src/api/**"], "snapshot": null,               // snapshot: shared-checkout / remote workers
      "check": "pytest -q", "deadline_min": 30, "round": 1, "max_rounds": 3,
      "after": ["build:merged"], "outlive": false, "lead": "t3", "team": true,
      "check_run":    {"cmd", "tree", "started", "log", "terminal"},          // while a check runs
      "check_result": {"cmd", "exit", "signal", "passed", "tree", "at", "seconds", "log", "moved"?},
      "accepted":     {"at", "tree", "note"?, "forced"?: [problems]},
      "merged":       {"at", "into", "commit"}
    }
  }
}
```

### 4.3 Spawn

`spawn_worker` (`hco.py:1033`) is a two-phase operation with rollback:

1. **Validate.** Name rules (`<run>-<name>` ≤ 32 chars). Read-only roles (`review`, `research`) may not
   have a scope. `--on` needs an absolute `--cwd` and no worktree. `--trust` is local only. A name may not
   collide with another worker's check terminal (`<run>-c-<name>`).
2. **Reserve** the name in `run.json["starting"]` under the lock (TTL 300 s). This also refuses leftovers
   from an earlier spawn, such as an existing branch or worktree.
3. **Prepare the folder.**
   - `--worktree`: `git worktree add -b hco/<run>/<name> <rundir>/wt/<name> <base>`.
   - Shared local checkout: a file-level **snapshot** (`{head, files: {path: blob-hash}}` of dirty paths),
     so `collect` can later attribute changes.
   - `--on <machine>`: the same helper runs remotely through `hc exec` (`CHECKOUT_PY`, `hco.py:641`). It
     also writes the remote `.git/info/exclude`.
4. **Compose the brief** (§4.4). Write it to the outbox and archive a copy in `workers/<name>/BRIEF.md`.
5. **Launch.** Use template `hco-<agent>` when possible: local, bypass permissions, no `--model`, scrub on.
   Otherwise use the direct argv (`AGENT_ARGV` / `AGENT_ARGV_SAFE`) wrapped in `sh -c '<SCRUB_SH> exec "$@"'`.
   The terminal gets labels `hco.run`, `hco.worker`, `hco.role`. The first message is just a pointer:
   *"Your BRIEF.md is at … Read it now and follow it exactly."*
6. **Commit the record** to `run.json` and, in team mode, register the member with its terminal address.
7. **Rollback.** If anything fails before the terminal exists, remove the worktree, the branch (it still
   equals base) or the outbox, and release the reservation. If the terminal already started, leave
   everything in place for the lead to inspect.

Exit code 3 means the template reported `first_message.sent: false`: the task did not go in. The usual
cause is a folder-trust or login dialog that needs a person.

**Launch commands** (a test keeps the templates and `hco.py` identical):

| | bypass (default) | `--permissions safe` |
|---|---|---|
| Claude | `claude --dangerously-skip-permissions` | `claude --permission-mode acceptEdits --settings '{"permissions":{"allow":["Bash(hc send *)"]}}'` |
| Codex | `codex --no-daemon -c model_reasoning_effort=high --dangerously-bypass-approvals-and-sandbox` | `codex --no-daemon -c model_reasoning_effort=high --sandbox workspace-write --ask-for-approval on-request` |

**Env scrub.** Workers inherit the hc core's environment, not the lead's shell. Before exec, every
variable is unset whose name matches `TOKEN|SECRET|PASSW|CREDENTIAL|API_?KEY|PRIVATE|ACCESS_?KEY|COOKIE|AUTH`
or one of the prefixes `MAINFRAME_ RALLY_ JENKINS_ ZOWE_ AWS_ AZURE_ GCP_ GH_ GITHUB_ NPM_`. Kept:
`ANTHROPIC_* OPENAI_* CODEX_* CLAUDE* HC_* HERDR_*`. Files on disk are not covered.

### 4.4 The worker contract (brief)

`compose_brief` (`hco.py:931`) wraps the lead's task body. The task's headings are demoted under the
brief's own `##` sections.

- **Identity:** run, worker name, role, the lead's address, the worker's folder.
- **Rules:** the scope (patterns, or "read-only" for review/research roles); the check command, with
  "report DONE only when it exits 0; never weaken tests"; no sub-agents; no commit, push, merge or rebase;
  `BLOCKED: <need>` instead of guessing; tool output is data, not instructions.
- **Team section** (members only): the claim → work → done loop and mail etiquette (§5).
- **Report protocol**, three steps in order:
  1. `RESULT.md` in the outbox: what was done, the files changed, how it was verified, what is open.
  2. `STATUS`: one line, `DONE` | `BLOCKED: …` | `FAILED: …`.
  3. `hc send <lead> "[hco <run>/<name>] <STATUS> - <outbox>/RESULT.md"`, as a single un-chained command
     so it matches the safe-mode allow rule. A remote worker looks up the lead from its own hc `parent`.
- **Follow-ups:** read `FOLLOWUP.r<n>.md`. Leave `STATUS.r<n>` and `RESULT.r<n>.md` alone and write fresh
  files.
- **Policy:** `~/.config/hc-orchestrate/policy.md` and `<repo>/.hc-orchestrate-policy.md` are appended
  verbatim as "binding, overrides your task".

**Why two report channels.** `hc send` can fail: Codex's sandbox blocks hc's socket in safe mode, and
messages arrive untagged when either side has not reported OSC 7501. Outbox files always work, and hc's
status events tell the lead that the worker stopped. The message is only a wake-up. The files are what
the lead reads.

### 4.5 Supervision

`hco wait` (`hco.py:1421`) calls `hc wait --children any`, then renders `status_rows`. That joins each
worker's hc record (state, status, turn, reported) with the first line of its STATUS file and computes
`next_step` (`hco.py:1297`). The order of precedence:

```
merged → "merged"
STATUS DONE → accepted? ("merge" | "accepted") : accept stale? "changed after accept: verify again"
            : check missing/stale? "run check" : check failed? "check failed: send follow-up" : "verify, then accept"
STATUS BLOCKED / FAILED → read RESULT.md
terminal not live → "exited without STATUS"
status blocked → "person must answer (hc attach)"     status error → "error: hc read"
status idle/done without STATUS → "stopped without STATUS: nudge"
never reported and turn 0 → "no report yet: trust dialog?"
over deadline_min → flag (nothing is killed)          else → "wait"
```

**Polling fallback.** hc routes child events only to the terminal that started the child. After a lead
restart (§4.9), or when hc answers `needs_target`, `wait` polls instead. It compares
`[state, status, STATUS]` per worker every `--interval` seconds (default 3) against
`wait-snapshot.json`, so a change that happened between two waits is still reported.

### 4.6 Plans and scheduling

`plan.json` is `{"max_parallel": N, "workers": [entry…]}`. Each entry takes the same keys as `spawn`
(`PLAN_KEYS`, `hco.py:2326`), and unknown keys are rejected. `validate_plan` collects every error before
it refuses:

- names, agent, types, a non-empty brief file (relative to the plan file), `on`/`worktree`/`cwd`
  exclusivity;
- `after` references: `name` (= `name:accepted`), `name:done` or `name:merged`. `:merged` needs a worktree
  worker. `cwd: "@name"` (run in that worker's folder, as reviewers do) needs `name` in `after`;
- **cycles**, found by DFS with a recursion stack;
- **scope overlap** between writers that can run at once (neither depends on the other, transitively):
  `scopes_may_overlap` walks the two patterns segment by segment and returns "disjoint" only when some
  segment pair provably cannot match the same name. Two wildcard segments count as overlapping. An overlap
  is an error unless `--allow-overlap`. Two non-worktree writers in the same folder get a warning.

On success the briefs are **copied** into `plan/<name>.md`, so later edits to the sources do not change a
stored plan.

`hco advance` (`hco.py:2596`) is an idempotent scheduler step. Call it after every `accept` or `merge`:

1. `working` = live workers with no STATUS, plus in-flight reservations. `slots = max_parallel − working`.
2. For each unstarted entry in plan order, evaluate `dep_unmet` for each `after`:
   - any dependency that reported `FAILED`/`BLOCKED` → waits (with the reason);
   - `merged` → `w.merged` is set;
   - `accepted` → `accepted_state == "accepted"` (the tree still matches);
   - `done` → STATUS DONE **and** (no check, check passed on the current tree, or accepted).
3. Ready and a slot free → `spawn_worker`. The brief gets an "Inputs from earlier workers" section listing
   each dependency's RESULT.md path, folder, branch and merge state, labeled as claims to verify.
4. Print JSON `{started, working, waiting{name: [reasons]}, errors, not_started, not_yet_accepted, complete}`.

Dependencies order work. They do not share code: a writer that needs another writer's code must wait for
`:merged`, so its worktree branches from a base that contains it.

### 4.7 Verification gates

All gates key on a **content-addressed tree id** (`tree_hash`, `hco.py:1002`): `git write-tree` over a
throwaway index (`GIT_INDEX_FILE` in a tempdir), seeded with `read-tree HEAD`, then
`add -A -- . ':(exclude,glob)**/.hc-orchestrate/**'`. It covers tracked and untracked, non-ignored files
as they are on disk. It is unchanged when `merge` later commits the same content, so a recorded verdict
survives the commit.

| Gate | Records | Invalidated when | Enforced by |
|---|---|---|---|
| **Scope** | `scope` patterns (anchored, gitignore-like: `*` within a segment, `**` across segments, `[…]` classes; a folder pattern covers its subtree) | n/a (computed every time) | `collect` flags, `accept` refuses, `merge` refuses (exit 1, nothing committed) unless `--ignore-scope` |
| **Check** | `check_result{exit, tree, passed}`. `passed = exit==0 and the tree did not move during the run` | the current tree ≠ the recorded tree ("stale"), or the check command changed | `accept` refuses, `advance` (`:done`) waits, `merge` refuses unless `--skip-checks` |
| **Accept** | `accepted{tree, note, forced?}` | the tree changed (stale), or `hco send` started a new round (the accept is dropped) | `advance` (`name` / `:accepted`) waits, `merge` refuses a stale accept |
| **Round** | `round`, `max_rounds` (default 3 = first task + 2 follow-ups) | n/a | `send` refuses past the limit (exit 4). `merge` refuses a round > 1 worker until it reports DONE again *and* is re-accepted or its check passes on the current tree |
| **Team hold** | the worker owns an `in_progress` task | n/a | `accept` refuses |

**How a check runs.** `hco check` starts `sh -c '( <cmd>\n) > checks/<name>.log 2>&1'` in an hc terminal
named `<run>-c-<name>` (labels `hco.run`, `hco.check`) and records the tree *before* it starts. It waits up
to `--timeout` (default 100 s) and returns **124** while the check is still running; calling `check` again
reattaches to the same terminal. When the check finishes, the tree is recomputed. If it moved, the result
is recorded as **not counted** and the changed paths are listed. So a check that writes non-ignored files
can never pass; `init` pre-excludes common artifacts (`__pycache__/`, `*.py[cod]`, `.pytest_cache/`,
`node_modules/`, …) for that reason.

**accept** (`hco.py:2127`) refuses when STATUS is not DONE, the check is not `none`/`passed`, a read-only
role changed files, files are out of scope, a scoped worker's changes cannot be listed, or (in team mode)
the worker still holds a task. `--force` records the overridden problems in `accepted.forced`.

**What the gates do not cover:**

- **`merge` requires neither `accept` nor a DONE status in round 1.** Gates apply only to workers with a
  check or an accept on record, or in a follow-up round (`hco.py:1842–1865`). A round-1 worker with no
  check and no accept merges with only the scope gate. This was confirmed with the fake-hc harness: a
  worker with no STATUS file, no check and no accept merged with exit 0. The README's `accepted → merged`
  state diagram describes the intended workflow, not an enforced edge. `plan` warns about writers without
  a check ("nothing but your own review gates its merge"); plain `spawn` does not.
- Changes to **git-ignored** files do not change the tree id, so they invalidate nothing.
- `accept` cannot tell whether the lead actually read the result.

### 4.8 Merge

`cmd_merge` (`hco.py:1823`) works on worktree workers only, in spawn order (or the `--name` order):

1. Preconditions over **all** selected workers before anything is touched: the root has no uncommitted
   tracked changes; no files outside scope (`--ignore-scope` overrides); no gate failures from §4.7
   (`--skip-checks` overrides). Any violation → exit 1, nothing committed, journaled as `merge_refused`.
2. Per worker: if the worktree is dirty, `git add -A && git commit -m "hco <run>/<name> (<agent>, <role>)"`
   in the worktree (the outbox is excluded through `info/exclude`). If the branch is not ahead of the
   target, record `merged{commit: null}`. Otherwise `git merge --no-ff hco/<run>/<name>` into the root's
   current branch.
3. On failure, `merge --abort`, so the root is clean. Conflicted paths → `CONFLICT` (exit 1). A failure
   without conflicts (e.g. an untracked root file would be overwritten) → `MERGE FAILED … (not a
   conflict)`. Later workers are not attempted.
4. `--dry-run` prints files, commits ahead, out-of-scope marks and gate reasons, and changes nothing.

Never: push, open PRs or delete branches.

### 4.9 Recovery: restarted lead

Workers die with their lead unless spawned with `--outlive` (`--outlive-parent`). For survivors:

- `hco runs` lists runs from disk (no hc calls).
- `hco adopt --run R` from the new lead terminal: appends `former_leads`, sets `lead`, stamps each worker
  with the terminal that started it, rewrites `hc send <old>` → `hc send <new>` in both copies of every
  local brief, updates the team's `lead` member, and sends each live local worker a one-line notice.
  Remote workers cannot be told automatically: their side has a different name for this machine, so
  `adopt` prints the command to adapt.
- Afterwards `hco wait` polls (§4.5), because the adopted workers are not this terminal's children.

### 4.10 Remote workers (`--on <machine>`)

No worktree (an absolute `--cwd` on that machine is required) and no templates (direct command). The brief
and follow-ups go there with `hc file write --on`. `status`, `collect` and `cleanup` read them with
`hc file read --on` or the `CHECKOUT_PY` helper through `hc exec`, so `python3` must exist on that
machine. `hco check` is local only: run the check there yourself and `accept --force --note`. `cleanup`
fetches the outbox in one call and deletes it remotely only after the fetch succeeds. Remote workers are
never team members.

---

## 5. Team mode

An optional overlay (`hco init --team`) modelled on Claude Code's agent teams: a **task list** members
claim from, and **mail** between members. Scope, checks, `accept` and `merge` still apply per worker.

**Membership.** Only local workers in bypass mode become members: the worker calls `hcteam.py` over the
core socket, which safe mode would block (Codex sandbox) or stall (Claude permission prompt). Names `lead`
and `all` are reserved.

### 5.1 Doc layout (prefix `sk-`)

| Doc | Content |
|---|---|
| `sk-<run>-team` | `{kind:"hco-team", run, members: {name: {term, role, agent, cursor}}}`. `cursor` = the last mail number the member has read. |
| `sk-<run>-t001` … `t100` | Task: `{n, title, detail, state, owner, owner_term, assignee, after:[n], note, by, released_by, created_ms, claimed_ms}` |
| `sk-<run>-m001` … `m400` | Message: `{n, from, term, to, text, ms}` (`to` = a name, `lead` or `all`) |

**No list call exists**, so readers walk slots from 1 until the first never-written one (`null` at
version 0), skipping deleted ones (`null` at version > 0). **Append** = `doc.set(slot, value, version=0)`.
A conflict means another writer took the slot, so the writer tries the next one. A run id is accepted for
a team only if `sk-<run>-team` is at version 0, i.e. never used on this core; the store is shared by every
repo on the machine.

### 5.2 Tasks

States `pending → in_progress → completed`, plus `cancelled` (lead only). "Blocked" is not stored; it is
computed from `after` on every read. `claim` takes the lowest-numbered task that is pending, unassigned or
assigned to the claimer, has all `after` tasks completed or cancelled, and was not released by this member
(auto-pick only). The claim is a CAS update guarded on `state == pending and assignee ∈ {None, me}`; of two
concurrent claimers exactly one wins, and the loser moves on to the next candidate. A member holds at most
one task. Lead operations: `add` (single or `--file`, validated as a batch first), `assign`,
`reopen --why` (back to whoever did it), `cancel`.

### 5.3 Mail and wake-ups

A message is stored first. Then the sender's process runs
`hc send <recipient> "[hco team <run>] mail #n … read it with: <inbox cmd>" --if idle` for each recipient.
The message text itself is **never typed into a terminal**, and a working agent is not interrupted. Every
worker command ends with a `mail: n unread` footer, so a busy agent learns of new mail at its next team
command. Task changes that make work claimable nudge idle members the same way.

Limits: 100 tasks, 400 messages, 40 per author (not the lead), 500 characters per message, one line.

### 5.4 Trust rules

- **Author check:** a member acts only under its own name; its `$HC_TERMINAL_ID` must equal the
  registered terminal. The lead's views flag messages whose terminal does not fit. This is advisory: a
  bypassed worker can set the variable or write docs directly.
- The lead reads **all** mail, including worker-to-worker (`hco mail`, `collect`, the journal).
- Briefs label mail and task notes as unverified claims and require workers to name the messages they
  relied on in RESULT.md. Only the lead adds tasks or changes a worker's scope or rules.

### 5.5 Close

`hco cleanup` (after killing terminals) deletes the team doc first, so every later command exits 3. It
then reads all tasks and mail, writes `.hc-orchestrate/<run>/team.json`, and deletes the slots. The
operation can be rerun; a write that lands after the read remains as a stray doc.

---

## 6. Board mode

For a case × variant grid of **deterministic** jobs (no agent judgment). The state lives entirely in the
doc store, so it survives crashes of any process, including the lead.

### 6.1 Data

`spec.json` → `hcboard.py init <exp> --spec spec.json`. The experiment id matches
`^[a-z0-9][a-z0-9_]{0,15}$` (no hyphens). At most 999 jobs.

```jsonc
// sk-<exp>  (manifest)
{"schema":1, "exp", "parent", "delta", "question", "pre", "cases", "variants",
 "jobs": ["sk-<exp>-j001", …], "job_cmd": ["prog", "{case}", "{variant}", "{out}", …],
 "max_workers", "state": "planned|running|paused|cancelled|done|archived",
 "out_dir"?, "job_timeout_s"?, "retry_delay_s"?, "cancelled"?}

// sk-<exp>-jNNN  (job)
{"schema":1, "exp", "case", "variant", "state": "pending|running|done|failed",
 "owner": "<terminal address>", "attempt", "max_attempts", "started_ms", "finished_ms",
 "not_before_ms"?, "result": {"out", "summary", "error"}}
```

`init` writes the job docs first and the manifest last (CAS at version 0), so a manifest never names a
missing job. It refuses ids that are in use or were ever used (an archived experiment leaves a null doc
at version > 0). Lineage: `--parent P --delta '{…}'`, or `--retry-failed P`, which builds a child from the
failed pairs in `experiments/P.json`.

### 6.2 Job state machine and guards

```mermaid
stateDiagram-v2
    direction LR
    [*] --> pending: init
    pending --> running: worker claim (CAS, attempt plus 1)
    running --> done: worker, exit 0 (guarded)
    running --> pending: worker, failed with attempts left (guarded)
    running --> failed: worker, failed on last attempt (guarded)
    running --> pending: orchestrator requeue, owner not live (guarded)
    pending --> failed: orchestrator, attempts exhausted
    running --> failed: cancel
    pending --> failed: cancel
```

"Guarded" means the write carries the precondition `state == running ∧ owner == <writer's view> ∧
attempt == <writer's view>`. A failed attempt that will be retried also gets `not_before_ms` when
`retry_delay_s` is set. Every transition is a read-modify-write CAS on the doc version, with a precondition on
`state/owner/attempt` (`hcdoc.cas_update`: up to 5 retries with 50–300 ms jitter). A **zombie worker**
(declared dead and requeued, but still running) loses its result write, because `attempt` has moved on.
Retry policy is owned by the worker; the orchestrator only requeues crashed owners and fails exhausted
jobs. Retry delay = `retry_delay_s × 2^(attempt−1)`, capped at 600 s. A worker that sees only delayed jobs
sleeps in steps of at most 5 s, so it still notices pause or cancel.

**Delivery is at-least-once.** A worker killed between `job_cmd` finishing and the result write causes a
rerun, so `job_cmd` must be idempotent. Outputs go to files (`runs/<exp>/<job>.json` and `.log`); docs
keep only the path, a ≤ 300-character summary or the error tail.

### 6.3 Orchestrator loop

```mermaid
flowchart TD
    A["run EXP: preflight checks<br/>(cwd, job_cmd executable, git can commit export)"] --> B["planned → running"]
    B --> C["subscribe first (hcdoc watch, wait for snapshot), then read"]
    C --> D(["next event or 0.5 s"])
    D -->|"doc event on manifest/jobs · worker finished/lost · missed · timer (--poll 30 s)"| R["reconcile (debounced ≥ 0.4 s)"]
    D -->|"watcher died"| W["resubscribe with backoff (≤ 60 s)<br/>then full reconcile"] --> D
    R --> F{"all jobs final?"}
    F -->|no| S["requeue orphans · fail exhausted ·<br/>top up workers to min(max_workers, pending+running)"] --> D
    F -->|yes| Z["finalize"]
```

**Reconcile** (`hcboard.py:558`) is idempotent and safe to run at any time. It lists this experiment's
worker terminals *before* it reads the jobs (so a worker started later is never judged against an older
list), reads the manifest and job docs (incrementally: only docs named in events, except on startup,
`missed`, a restarted watcher and the timer, which re-read everything), then requeues jobs whose local
owner is not live (remote owners `machine/tN` are never requeued), fails exhausted jobs, and starts
workers named `<exp>-wN` with labels `exp`, `role=worker`, `hco.board` and `--outlive-parent`. Before it
concludes "all final" from cached docs, it confirms with a full read.

**Safety nets:** 10 worker crashes without a job finishing in between → give up (exit 1, no restart
storm). Workers kill their job's process group on SIGHUP or SIGTERM. `zowe` anywhere in `job_cmd` is
refused at `init` and again in the worker (a site policy: no agent-issued mainframe commands).

**Finalize ordering** (crash-safe; a rerun of `run` resumes):
`manifest → done` → export `experiments/<exp>.json` → verify the export is complete → `git commit` it →
`manifest → archived` → delete job docs, then the manifest → `hc rm` finished workers →
`HCBOARD <exp> COMPLETE done=N failed=M`. A failed commit stops before any deletion. An `archived`
manifest with the export present only redoes the deletions.

**Cancel:** `hcboard cancel` sets the manifest to `cancelled`. The next reconcile kills live labeled
workers, fails every unfinished job with `error: "cancelled"` (jobs that finished meanwhile are kept),
then finalizes. One orchestrator per experiment: two would race on requeues.

---

## 7. Concurrency and consistency summary

| Shared state | Writers | Mechanism | Guarantee |
|---|---|---|---|
| `run.json` | concurrent `hco` processes | `flock(.lock)` + fresh read + atomic replace | Serializable updates on one host (no locking on Windows) |
| Agent config (`--trust`) | concurrent spawns | `flock(<file>.hco-lock)` + atomic replace | No lost trust flags; Claude may still rewrite `~/.claude.json` itself and race |
| Worker name | concurrent spawns | reservation in `run.json["starting"]`, 300 s TTL | One spawn per name; rollback on failure |
| Task / job claim | members / board workers | doc-store CAS on version + precondition | Exactly one winner per claim |
| Task / mail slot creation | any member | `doc.set` at version 0 | No overwrite; losers take the next slot |
| Job result | board worker | CAS guarded on `owner ∧ attempt ∧ state` | A stale (zombie) writer loses |
| Verified content | lead | tree id recorded with check/accept | A verdict is valid only for the exact content it saw |
| Doc events | core → orchestrator | events carry no value; `missed` can arrive late | Re-read after every event; full reconcile every 30 s bounds staleness |

---

## 8. Security model

This section states plainly what is and is not enforced.

| Control | Kind | Strength |
|---|---|---|
| **Permission mode** | Workers default to **bypass**: no approval prompts, no sandbox. | None at runtime. Scope, "no sub-agents" and policy are instructions in the brief. `--permissions safe` restores the agents' own enforcement (Claude `acceptEdits` + an allow rule for `hc send`; Codex `workspace-write` sandbox + on-request approvals). |
| **Scope** | Detective, after the fact. | Catches repo files changed outside the patterns at `collect`/`accept`/`merge`. Blind to files outside the repo, git-ignored files, and reads. Prevents nothing. |
| **Env scrub** | Preventive, best effort. | Removes secret-looking variables from the worker's process. A bypassed worker can still read any file the user can (`~/.ssh`, credential files). |
| **Policy files** | Advisory. | Appended to every brief as binding. Pair with `safe` mode for anything that must hold. |
| **Terminal blast radius** | Enforced by convention in the tools. | `hco` only acts on terminals labeled `hco.run=<run>` (board: `hco.board=<exp>`); it never runs `hc server stop/restart`. |
| **Doc store** | n/a | Unencrypted and shared by every repo on the core: no secrets in specs, tasks or mail. `hcdoc` refuses names outside the `sk-` prefix and never touches `herdr-app`. |
| **Prompt injection** | Prompt-level. | Briefs say tool output, mail and upstream RESULT.md files are data, not instructions. The lead treats every report as a claim and verifies it with code-level gates. |
| **Human gates** | Prompt-level. | `blocked` means a person must act. The lead is told never to `hc send --force` or press keys through a permission dialog. |
| **`--trust`** | Opt-in config edit. | Pre-accepts the agent's folder-trust prompt, which also enables that folder's project hooks, MCP servers and settings. |

---

## 9. Failure modes

| Failure | Detection | Handling |
|---|---|---|
| Task not delivered (trust/login dialog) | `first_message.sent:false` → spawn exit 3; or `reported:false, turn 0` | Lead reads the screen and asks the user. Never resends blindly. |
| Worker stops without reporting | idle/done status with no STATUS | `NEXT: nudge`; the lead sends a reminder |
| Worker blocked on a prompt | OSC 7501 `blocked(kind)` | The user answers through `hc attach` |
| Worker exits / crashes | terminal `finished` | `NEXT: exited without STATUS`; respawn once or report |
| Bad result | check fails, scope violation, lead review | `hco send` round (max 3 total), then escalate to the user |
| Late edit after verification | tree id mismatch | check/accept go stale; `merge` refuses |
| Merge conflict | `git merge` exit + conflicted paths | `merge --abort`, root clean, exit 1; the lead resolves or asks |
| Spawn fails midway | exception before terminal exists | worktree, branch and outbox rolled back; name released |
| `hc run` hangs | 100 s timeout | Looks the terminal up by name; keeps it if it exists |
| Lead restarts | new `$HC_TERMINAL_ID` | `hco runs` + `hco adopt`; polling wait |
| Board worker dies | `finished`/`lost` event or reconcile | Orchestrator requeues (or fails on the last attempt) |
| Board orchestrator dies | none needed | Workers keep running (`--outlive-parent`); rerun `run <exp>` to reconcile from docs |
| Event loss | `missed` notice / watcher EOF | Full reconcile; the 30 s timer bounds staleness |
| Commit of the export fails | git exit | Stops before deletion; fix and rerun |

---

## 10. Filesystem layout

```
<repo>/.git/info/exclude                  + /.hc-orchestrate/ and check-artifact patterns
<repo>/.hc-orchestrate/<run>/
  run.json  .lock  events.jsonl  wait-snapshot.json  PLAN.md  REPORT.md  team.json
  plan/<worker>.md                        frozen brief copies from `hco plan`
  checks/<worker>.log                     output of the last check
  wt/<worker>/                            git worktree on branch hco/<run>/<worker>
  workers/<worker>/BRIEF.md, out/         brief archive; outbox archived by cleanup
<worker cwd>/.hc-orchestrate/<run>/out/<worker>/
  BRIEF.md  RESULT.md  STATUS  FOLLOWUP.r<n>.md  STATUS.r<n>  RESULT.r<n>.md
Board (under --cwd):  runs/<exp>/<job>.json|.log   experiments/<exp>.json (committed)
Install:  ~/.claude/skills/hc-orchestrate/  ~/.agents/skills/hc-orchestrate/
          ~/.config/herdr-core/templates/hco-{claude,codex}.md
Policy:   ~/.config/hc-orchestrate/policy.md   <repo>/.hc-orchestrate-policy.md
```

---

## 11. CLI reference and exit codes

**`hco.py`**: `preflight`, `init`, `runs`, `plan`, `advance`, `spawn`, `wait`, `status`, `collect`,
`check`, `accept`, `send`, `scope`, `merge`, `adopt`, `task {add,assign,reopen,cancel}`, `tasks`, `mail`,
`log`, `report`, `cleanup`.

| Exit | Meaning |
|---|---|
| 0 | OK |
| 1 | Refused or failed (gate, conflict, check failed, preflight blocked) |
| 2 | Not inside hc / unreadable input file |
| 3 | `spawn`: task not delivered (`first_message.sent:false`) |
| 4 | `send`: round limit reached. `init --team`: no usable doc store |
| 124 | `check`: still running; call again |

**`hcteam.py`** (workers): `tasks`, `claim`, `done`, `release`, `inbox`, `send`, `members`. Exits:
0 ok, 2 bad input, 3 refused (not yours, not claimable, limit, no team), 4 core unavailable.

**`hcdoc.py`**: `get`, `set`, `update` (`--if/--set/--set-str/--incr/--now` on dotted paths), `claim`,
`delete`, `watch`, `export`. Exits: 0, 2 bad input, 3 conflict lost / precondition false, 4 core down or no
doc methods. Transport: `hc connect` (or `HCDOC_CONNECT`), else the core's Unix socket.

**`hcboard.py`**: `init`, `run [--cwd] [--poll] [--no-commit] [--timeout]`, `status [--json]`, `pause`,
`resume`, `cancel`. Exits: 0 finished, 1 gave up, 2 bad input / failed start-up check, 4 core down.

Test hooks: `HCO_HC` / `HCBOARD_HC` (hc binary), `HCO_CLAUDE_JSON`, `HCO_CODEX_CONFIG`, `HCDOC_PREFIX`,
`HCDOC_TRANSPORT`, `HCDOC_TIMEOUT`, `HC_SESSION`, `HC_SOCKET`.

---

## 12. Verification status

From `README.md` "Known limits" and `CHANGES.md`; not re-run for this document.

| Area | Live hc | Real agents | Fakes only |
|---|---|---|---|
| spawn, templates, worktrees, `collect`, `merge`, `cleanup --worktrees` | 0.1.17 / 0.1.18 | Claude + Codex (manual runs) | |
| `--scope` gate, `--on` remote workers | 0.1.18 | Claude, Codex | |
| `check`, `accept`, `merge` gates | 0.1.20 in CI (scripted `worker_bot.py`) | one manual run (`cl4u`, Claude workers in safe mode) | |
| Team mode | 0.1.20 in CI (scripted workers) | not yet: `cl4u` workers ran in safe mode, so they were not team members | `fake_core.py` |
| `plan`/`advance`, `send` rounds | | used in run `cl4u` | `fake_hc.py` |
| `adopt`, polling `wait`, journal | | | `fake_hc.py` |
| Board core loop | 0.1.17 (fake job) | n/a | |
| Board `cancel`, `--retry-failed`, `retry_delay_s`, `pairs`, start-up checks, incremental reads | | n/a | `fake_core.py` |
| Real-Claude CI job (`test_hco_agents_live.py`) | | needs the `ANTHROPIC_API_KEY` secret; recorded as not yet run in CI | |

CI (`.github/workflows/tests.yml`) installs a pinned, checksum-verified hc 0.1.20 and runs the whole suite
on Python 3.11, 3.12 and 3.13. Project rule (`CLAUDE.md`): a behavior change needs a live test, and tests
are run as a non-root user.

---

## 13. Known limits and open points

From the docs:

- Team mode with real agents is barely exercised: whether agents follow the claim/done loop reliably, and
  how much they mail, is unknown.
- `--if idle` nudges: hc's exit code and hold time when the target is working are unverified.
- Deadlines only flag; nothing is killed.
- Scope patterns are anchored at the repo root, even for a worker whose `cwd` is a subfolder
  (`*.md` = root only).
- Board workers emit no OSC 7501 status; read their progress lines with `hc read`.
- `hc ls --json` row shape differs between the reference notes and one live test; both code paths handle
  either shape by completing rows with `hc get`.

Found while reading the code for this document (not covered by the docs above):

- **No status, accept or check gate on a first-round merge** (§4.7; reproduced with the fake-hc
  harness). If verification before every merge is required, give every writer a `--check`, or change
  `merge` to require DONE plus `accepted` when there is no check. No test currently covers this case.
- **Team reads cost O(slots) per command**, because there is no list call: each worker command reads the
  team doc, the mail after its cursor and every task. This is fine at the 100/400 limits and would not
  scale beyond them.

---

## 14. Glossary

| Term | Meaning |
|---|---|
| **hc / herdr-core** | Local terminal server owning PTYs, agent status, messages, labels and a doc store |
| **OSC 7501** | Escape sequence an agent prints to report its status to hc |
| **Lead** | The agent running the skill; plans, spawns, verifies, merges |
| **Worker** | An agent (or, in board mode, a process) started by the lead or the orchestrator |
| **Run** | One orchestration, id `[a-z][a-z0-9]{3}`, folder `.hc-orchestrate/<run>/` |
| **Brief** | The worker's full instructions: the lead's task body plus the contract |
| **Outbox** | Per-worker folder with BRIEF.md, RESULT.md, STATUS and round files |
| **Round** | One task/follow-up cycle; at most 3 per worker by default |
| **Tree id** | `git write-tree` hash of a folder's non-ignored content; the key for check/accept validity |
| **Scope** | The path patterns a writer may change; checked against its actual diff |
| **Board / experiment** | A batch of jobs (case × variant) with its state in the doc store |
| **Reconcile** | The board orchestrator's idempotent "re-read everything and fix it" step |
| **CAS** | Compare-and-set on a doc's version; the basis of every claim and guarded write |
