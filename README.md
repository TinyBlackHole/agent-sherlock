# Agent Sherlock

A small command-line application.

Source: https://github.com/TinyBlackHole/agent-sherlock

## Install

Install Agent Sherlock with:

```bash
curl -fsSL https://sherlock.tinyblackhole.com/install.sh | sh
```

For local development, install the CLI in editable mode from this project directory:

```bash
python3 -m pip install -e .
```

## Usage

Print the current version:

```bash
sherlock -v
```

Expected output:

```text
0.1.0
```

Run without arguments (or with `-h`) to list available commands:

```bash
sherlock
```

Subcommands are invoked as `sherlock <command> [options]`.

## Adding a command

Each subcommand lives in `src/agent_sherlock/commands/`. Copy `_template.py`,
implement your command, and register its `Command` instance in
`commands/__init__.py`'s `COMMANDS` list. A handler returns an `int` exit code
(`0` = success).
