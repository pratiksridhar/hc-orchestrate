# Orchestration patterns

Recipes for the lead. `hco` = `python3 <skill-dir>/scripts/hco.py`. Every
recipe assumes Phase 0 passed and you ran `hco init` (run id `k3x9` below).

Pick the smallest pattern that fits. More agents means more tokens, more
merge work and more things to verify.

---

## 1. Fan-out / fan-in — independent parts in parallel

Use when the task splits along file or module boundaries with no shared files.

```sh
hco spawn --run k3x9 --name api  --agent claude --role implement --worktree --scope 'src/api/**' --brief api.md
hco spawn --run k3x9 --name ui   --agent claude --role implement --worktree --scope 'src/ui/**' --brief ui.md
hco spawn --run k3x9 --name docs --agent codex  --role implement --worktree --scope 'docs/' --brief docs.md
hco wait  --run k3x9 --timeout 100      # repeat until all STATUS are DONE
hco collect --run k3x9                  # flags files outside each worker's --scope
```

- Partition scope by path before spawning and pass it as `--scope`; patterns that
  don't overlap mean no two writers can touch the same file without `collect` saying so.
- Shared types or interfaces: define them yourself first (or in a first,
  single worker), commit, then fan out from that commit (`--base <ref>`).
- Integrate in an order where each merge still builds and passes tests.

## 2. Build → cross-vendor review loop

Use for anything that matters. One vendor builds, the other reviews.

```sh
hco spawn --run k3x9 --name build --agent claude --role implement --worktree --brief build.md
# ... wait until build is DONE and you've checked its RESULT.md ...
hco spawn --run k3x9 --name rev1 --agent codex --role review \
  --cwd <build worktree path> --brief review.md
```

Review brief essentials: the diff to review (`git diff <base>...HEAD` in that
worktree), what "correct" means, severity scale, **read-only** (report, don't
fix), findings as `file:line — problem — why — suggested fix` in RESULT.md.

Then send the builder only the findings you agree with:
`hc send k3x9-build "Address these review findings: <path to rev1 RESULT.md>, items 1, 3, 4. Skip 2: <reason>."`
Stop after 2 rounds; leftover disagreements go to the user.

Swap vendors when Codex built it. `collect` flags a reviewer that edited files.

## 3. Variants / tournament — compare approaches

Use when the right approach is unclear, or to compare models or parameters on
the same task. Each variant gets the same task and acceptance check; only one
thing differs.

```sh
for v in a b c; do
  hco spawn --run k3x9 --name var-$v --agent claude --role implement \
    --worktree --brief variant-$v.md     # same task, different approach/params
done
hco spawn --run k3x9 --name var-x --agent codex --role implement --worktree --brief variant-a.md
```

Then judge them on evidence you produce, not on their self-reports: run the
same check in each worktree (in hc), diff sizes, failure counts, timing. Put a
comparison table in your report. Let the user pick, or pick with stated
reasons and keep the losers' branches.

## 4. Pipeline — stages that feed each other

research → design → implement → test. Each stage reads the previous stage's
RESULT.md path from its brief.

```sh
hco spawn --run k3x9 --name research --agent claude --role research --brief research.md
# wait, verify research RESULT.md, then:
hco spawn --run k3x9 --name impl --agent codex --role implement --worktree \
  --brief impl.md        # brief says: "Read <research RESULT.md path> first"
```

Verify each stage before starting the next; a bad research step poisons
everything after it. Kill finished stages (`hc kill`) to free resources, but
keep their outboxes.

## 5. Watcher — long-running processes without blocking

Servers, test suites, builds, CI. These are plain programs, not agents: they
report no status, so wait on output or exit.

```sh
hc run --bg --name k3x9-web --cwd <dir> --label hco.run=k3x9 -- bun dev
hc wait k3x9-web --output "ready" --timeout 100    # matches output printed after the wait starts
hc run --bg --name k3x9-ci --label hco.run=k3x9 --no-notify -- gh run watch 123 --exit-status
hc wait k3x9-ci --timeout 100; hc read k3x9-ci --lines 40
```

Label them `hco.run=<run>` so `cleanup` finds them. Hand a worker the
address of a server it should test against (`k3x9-web`) in its brief.

## 6. Remote machine

Run a worker where the code, data or hardware is.

```sh
hc machine ls                          # must already be added: hc machine add user@host --name box
hco spawn --run k3x9 --name perf --agent claude --role test \
  --on box --cwd /srv/app --brief perf.md
```

- `--cwd` is required with `--on` (remote paths start at the remote home).
- The brief is written there with `hc file write --on`; its outbox is there,
  and `status`/`collect` read it with `hc file read --on`.
- The agent CLI must be installed and logged in on that machine. Templates
  resolve there too, so `--on` uses the direct command, not a template.
- The worker reaches you via the `parent` that `hc get $HC_TERMINAL_ID` shows
  on its side; its brief says so.
- That machine needs `python3`. At spawn, `hco` runs a small helper there over `hc exec`: it
  adds the outbox and check artifacts to the repo's `.git/info/exclude` and snapshots the
  checkout. `collect` then lists the files whose content changed since the worker started
  (other workers in the same folder count too), checks them against `--scope` and flags a
  reviewer that changed any. (A local worker without `--worktree` gets the same snapshot.)
  Not a git repo: spawn warns and changes aren't tracked.
- `cleanup` fetches the outbox in one call, then deletes it on that machine; if the machine
  can't be reached it keeps it and says so.
- A new folder shows the agent's trust prompt there, and `--trust` can't pre-accept it
  remotely: the user accepts it with `hc attach machine/t7`.

## 7. Human gate — commands only a person may run

Some environments forbid agents from running certain commands (for example a
policy that only a person may send commands to a production or mainframe
system). Put the rule in `~/.config/hc-orchestrate/policy.md` so it reaches
every brief, for example:

```markdown
- Never run `zowe` or any command that talks to the mainframe.
  If the task needs one, write the exact command into RESULT.md under
  "Commands for a human to run", set STATUS to `BLOCKED: needs human-run command`,
  and report.
```

The lead collects those commands, shows them to the user, and the user runs
them (or stages them with their own approval tool). Results come back to the
worker with `hc send`. Note that wrapping a command in `hc run` sidesteps your
agent's own command permission rules, so policy must live in the briefs and
in each worker's own settings, not only in the lead's allow/deny list. Workers default to
bypassed permissions, so for this pattern spawn them with `--permissions safe` (and
still treat the policy as advisory: a bypassed or compromised worker can ignore it).

---

## Writing a good worker brief (task body)

```markdown
## Goal
One sentence: the outcome, not the steps.

## Scope
May change: src/auth/**, tests/auth/**
Must not change: anything else (read whatever you need).

## Context
- Relevant files: src/auth/session.ts (token refresh), docs/auth.md
- Known constraints: keep the public API of SessionStore unchanged.

## Done means
- `npm test -- tests/auth` passes.
- RESULT.md lists the changed files and the test output summary.

## Out of scope
Refactors outside auth; dependency upgrades.
```

Briefs fail most often by being vague about scope or about "done". If you
can't write "Done means" as a check, the unit is not ready to delegate.

Pass the same "May change" patterns to `hco spawn --scope` (here
`--scope 'src/auth/**' 'tests/auth/**'`). `spawn` then lists them in the worker's rules
with the instruction to report `BLOCKED: needs <path> (outside my scope)` rather than
stray, and `collect`/`merge` check the worker's actual files against them. A task that
needs a lockfile, a shared config or a test fixture should name it in the scope up front.
