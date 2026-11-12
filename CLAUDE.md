# Working on hc-orchestrate

## Tests: real hc and real Claude sessions

Fakes (`tests/fake_hc.py`, `tests/fake_core.py`) are for fast unit tests. A change is not finished until
it also passed against a real herdr-core and, when it changes what a worker does, with real Claude Code
workers:

- `python3 -I -m unittest discover -s tests -v` with `hc` on PATH runs the live tests too
  (`*_live.py`; each starts its own isolated hc session and stops only that one).
  `tests/test_hco_live.py` drives hco and team mode with scripted workers (`tests/worker_bot.py`).
- `HCO_LIVE_AGENTS=1` adds `tests/test_hco_agents_live.py`: real Claude Code sessions as workers. Needs
  `claude` logged in (or `ANTHROPIC_API_KEY`) and costs tokens.
- New behavior gets a live test, not only a fake one. If `hc` or `claude` is not available where you
  work, say so in the report and leave the change marked "not live-tested" in CHANGES.md.
- CI (`.github/workflows/tests.yml`) installs the pinned hc release and runs all of the above; the
  real-Claude job needs the `ANTHROPIC_API_KEY` repository secret.
- Run tests as a non-root user before saying they pass: CI does, and root hides permission bugs.
