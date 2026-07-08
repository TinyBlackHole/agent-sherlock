# AGENTS.md

Agent Sherlock — a Python CLI (`sherlock`), `src/` layout, entry point `agent_sherlock.cli:main`.

## Conventions (deliberate — don't break these)

- **Exit-code contract:** subcommand handlers return an `int` exit code; `main()`
  returns whatever the handler returns. Only `--version` and `--help` raise
  `SystemExit` (argparse). Don't add other `sys.exit()`/`SystemExit` paths.
- **Version is single-sourced** in `src/agent_sherlock/__init__.py`
  (`__version__`). `pyproject.toml` reads it dynamically — never hardcode a
  version there.
- **Adding a command:** copy `src/agent_sherlock/commands/_template.py`,
  implement it, and append its `Command` to `COMMANDS` in
  `commands/__init__.py`. `_template.py` is a template and stays unregistered.

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
