# AGENTS.md

Agent Sherlock — a Python CLI (`sherlock`), `src/` layout, entry point `agent_sherlock.cli:main`.

## Conventions (deliberate — don't break these)

- **Exit-code contract:** subcommand handlers return an `int` exit code; `main()`
  returns whatever the handler returns. Only `--version` and `--help` raise
  `SystemExit` (argparse). Don't add other `sys.exit()`/`SystemExit` paths.
- **Version is single-sourced** in `src/agent_sherlock/__init__.py`
  (`__version__`). `pyproject.toml` reads it dynamically — never hardcode a
  version there.
- **Delivery is at least once, never exactly once.** A message is stored before
  it is sent and marked delivered only afterwards. Keep each delivery to a
  single destination request so a retry cannot re-send half a message.
- **Queued rows are claimed, not just read.** `MessageRepository.claim()` takes
  ownership inside `BEGIN IMMEDIATE` under a worker ID and a lease; the delivery
  path must claim before sending and must release or finalize afterwards. The
  in-process lock alone does not protect two Sherlock processes.
- **Storage failures and delivery failures are different.** Streaming inputs
  (Discord Gateway) cannot replay, so a failure to persist raises
  `MessageIngestError` and must stop the watcher. A failure to deliver raises
  `PendingDeliveryError` and must keep it running.
- **Adding a command:** copy `src/agent_sherlock/commands/_template.py`,
  implement it, and append its `Command` to `COMMANDS` in
  `commands/__init__.py`. `_template.py` is a template and stays unregistered.

## Releases

`install.sh` installs the protected tag in `SHERLOCK_VERSION` (currently
`v0.1.0`), not the default branch. **That tag has to exist and be pushed**, or
the published installer fails. When bumping `__version__`, tag the release
commit with the matching `vX.Y.Z`, protect release tags against updates, and
update `SHERLOCK_VERSION` in `install.sh`.

## Tests

Run with plain `python3 -m pytest` — `pythonpath = ["src"]` is set in
`pyproject.toml`, so no install step is needed. Add a test for each new command.

## Linting & formatting

[ruff](https://docs.astral.sh/ruff/) handles both. Config lives in
`pyproject.toml` (`[tool.ruff]`); line length is the default 88.

```bash
ruff check .          # lint
ruff check --fix .    # lint + autofix
ruff format .         # format
```

CI (and pre-merge) expects `ruff check .` and `ruff format --check .` to pass.
