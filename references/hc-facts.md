# hc facts this skill relies on

Checked on 2026-11-03 against herdr-core `master` (= v0.1.17, released
2026-11-02): the agent manual (`crates/herdr-core/src/SKILL.md`, printed by
`hc --skill`), `docs/commands.md` (generated from the binary's help),
`docs/templates.md`, `schema/README.md`, and the Rust sources named below.
If your hc is newer and disagrees, `hc --skill` and `hc <cmd> --help` win.

## Identity and family

- `$HC_TERMINAL_ID` is set in every hc terminal: your address (`t7`). Remote
  terminals are addressed `machine/t7`. Names: 1–32 of `a-z 0-9 - _`, unique
  among live terminals.
- A terminal started from inside an hc terminal becomes its **child**.
  Children end when the parent's program ends, unless `--outlive-parent`.
- The core also exports `HC_TERMINAL_SOCKET` to programs (api/run.rs).

## Starting agents

- `hc run --bg --name N --cwd D [--label k=v]... -- <cmd|template> [task]`
- Always `--bg` for agents. Run agents interactively, never in print mode
  (`claude -p`, `codex exec`): they must stay steerable.
- **Template runs** (`hc run reviewer "task"`): core waits for the agent's
  first OSC 7501 report (default up to 60 s, `HC_READY_MS`), then pastes
  `template body + blank line + task`. The JSON result has
  `first_message: {sent, started?, reason?, turn?}` (templates.rs).
  `sent: false` means the task did NOT go in; `hc get <t>` keeps the reason.
- **Plain runs** (`hc run -- claude "task"`): the task is just an argument to
  the agent CLI, which starts with it as its first prompt. No
  `first_message` field. Both `claude "query"` and `codex "prompt"` start an
  interactive session with that prompt.
- `--json` on `run` prints the RunResult JSON (the terminal record plus
  `first_message`).
- Templates: `~/.config/herdr-core/templates/<name>.md`. Header is line-based
  (not YAML): `command` (required, run via `sh`), `description`, `ready`
  (`quiet` to skip waiting for a status report), `submit`, `stop`,
  `paste_gap_ms`. Body = opening instructions. Read fresh on each use.

## Talking

- `hc send <t> "<text>"` pastes and presses Enter. It waits ~3 s of quiet and
  holds while a person has an unsent draft in that terminal (up to 3 min;
  `waiting_for_person: true`). Don't resend while it waits.
- `--if idle|working` sends only in that status; `--wait` then waits for the
  turn it started; `--force` types even when `blocked` — don't use it to get
  past a permission dialog.
- `hc keys <t> enter|esc|up|ctrl+c|...` presses named keys.
- `hc interrupt <t>` presses the stop key (ends the turn, not the program).
- Tagging: a message from agent A to agent B arrives as
  `<hc from="t7 name">text</hc>` **only if both A and B have reported OSC 7501
  at least once** (notes.rs `tagged`). Otherwise it arrives as plain text that
  looks like user input. `notes = false` in config turns tags off entirely.
  That is why worker reports carry their own `[hco <run>/<name>]` prefix.

## Waiting

- `hc wait --children any` returns on the next child news (turn ended,
  blocked, error, exited) or a message typed to you (`reason: "message"`,
  `from` = sender address; the text itself is already in your input).
- `hc wait --children all` returns when none is working. A child that has
  never reported counts as working, and a child with a sent launch task
  counts as working until that turn passes.
- `hc wait <t>` waits for one terminal (status change, or exit for plain
  programs). `--output X` ends when X is printed after the wait starts.
- Default timeout 180 s (`wait_timeout` in config). Timeout =
  `ended: false`, `reason: "timeout"`: a check-in, nothing failed.
- WaitResult fields: `terminal, ended, reason, status, turn, exit_code,
  signal, children[], from` (wait.rs).
- `hc wait` only observes. Killing the hc process (e.g. your shell tool's
  timeout) loses nothing; run it again.
- **Trap:** Claude Code's Bash tool defaults to a 120 s timeout (max 600 s,
  `BASH_DEFAULT_TIMEOUT_MS`/`BASH_MAX_TIMEOUT_MS`). Pass `--timeout 100`, or
  raise the tool timeout and use a matching `--timeout`.

## Reading and state

- `hc read <t> --lines N` = screen + N lines of history (text). While the
  program is working/blocked/error, history reads return only the screen,
  marked incomplete.
- `hc get <t> --json` returns the **full** record: `address, name, labels, cwd,
  state (live|finished), status {state, kind, progress, app, title, msg} | null,
  turn, reported, waiting_for_person, parent, exit_code, reason`.
- `hc ls [--all] [--label k=v] --json` (`{"terminals": [...], "hidden_finished": n}`)
  returns **slim rows only** (verified live on 0.1.17): `address, agent, cwd, state,
  turn, where`, plus `status` as a bare string when reported. No `name`, `labels`,
  `reported` or `exit_code`; use `ls` to find addresses, `get` for details.
- Status words come only from the program via OSC 7501: `idle, working, done,
  blocked (kind: permission|question|auth), error`. hc never guesses from the
  screen. A program that never reported has no status (`reported: false`).
- `hc events [--label k=v] [--children-of t]` streams changes (JSON lines).

## Who reports OSC 7501

- Claude Code: since v2.1.295 (2026-10-08), automatically in terminals that
  support the protocol.
- Codex: native reporting (`idle`, `working`, `blocked`) merged in
  openai/codex#52725 on 2026-10-09; the release it shipped in wasn't stated.
  Check `reported` in `hco.py status` after a spawn.
- Pi: since 1.1.0.

## Codex specifics

- The herdr manual: "Codex must run with `--no-daemon` (including templates),
  or its commands execute outside hc." Since Codex 0.157.0 (2026-09-25) the
  TUI talks to a shared background server by default; `--no-daemon` runs it
  without one. Without it, `$HC_TERMINAL_ID` is missing in Codex's shell
  commands, so its hc calls have no caller and its reports can't route.
- `--sandbox read-only|workspace-write|danger-full-access`,
  `--ask-for-approval on-request|never`, `-c key=value` config overrides.
- Verified live: Codex's sandbox blocks `hc send` ("hc could not start its core: Operation not permitted").
  With `--dangerously-bypass-approvals-and-sandbox` (hco's default) it works: verified on hc 0.1.18.
  The worker contract writes STATUS/RESULT.md inside the workspace first, so
  the run still works if `hc send` is refused.

## Cleanup and safety

- `hc kill <t>` then `hc rm <t>`. Only your own terminals: `hc ls` shows all of
  the user's work.
- `--rm` on run removes a terminal once it exits and a waiting parent has the
  exit code; `--no-notify` keeps its exit note out of the parent's input.
- Never `hc server stop|restart` unless asked.
- Errors: `{error: {kind, message, hint}}` on stderr. Exit codes include
  3 not_found, 7 needs_target, 9 blocked, 10 status_mismatch, 11 finished;
  `run` without `--bg` exits 124 when its wait times out (program keeps running).

## Other machines

- `hc machine add user@host --name box`; `hc run --on box --cwd /abs/path ...`.
- `hc exec --on box -- <cmd>` runs one command without a terminal (exact
  output and exit code). `hc file read|write|edit --on box <path>`.
- `hc exec --json` prints `{code, signal, timed_out, truncated, stdout, stderr}` with stdout and
  stderr **base64-encoded**, and exits 0 even when the program failed: read `code`. `-i` feeds
  stdin; `--timeout` defaults to 60 s (max 600). No shell: wrap in `sh -c` if you need one.
  (Verified on hc 0.1.18.)
- `hc ls` has no `--on`; to list another machine's terminals, `hc exec --on box -- hc ls --json`.
- A remote child's parent is recorded as `<what that machine calls you>/t7`;
  the child sees it with `hc get $HC_TERMINAL_ID`.

## Sources

- herdr-core repo: https://github.com/herdrdev/herdr-core (manual, docs/, schema/)
- Site: https://herdr.dev/core/
- OSC 7501 spec: https://superlogical.com/rex/docs/build/program-status
- Codex OSC 7501 PR: https://github.com/openai/codex/pull/52725
- Claude Code skills / CLI / env vars: https://code.claude.com/docs/en/skills ,
  https://code.claude.com/docs/en/cli-reference , https://code.claude.com/docs/en/env-vars
- Codex skills: https://learn.chatgpt.com/docs/build-skills.md
