# Agent Sherlock

A small command-line application for securely connecting services and reacting
to new events.

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

## Connect Gmail

Before the first connection:

1. Create or select a project in the
   [Google Cloud console](https://console.cloud.google.com/).
2. Enable the Gmail API.
3. Configure the OAuth consent screen. If the app is in testing mode, add your
   Gmail address as a test user.
4. Create an OAuth client with the **Desktop app** application type and download
   its JSON file.

Then run the interactive connection menu:

```bash
sherlock connections
```

Choose **Connect Gmail**, enter the path to the downloaded JSON file, and finish
authorization in the browser. You can also run the direct command:

```bash
sherlock connections gmail connect --credentials /path/to/credentials.json
```

Sherlock requests only the `gmail.metadata` scope. It can read message headers
and labels to report the sender, subject, and date, but it cannot read email
bodies, modify mail, or send mail. The downloaded OAuth client file is read
during connection and is not copied into Sherlock's configuration.

Check once for mail received since the previous check:

```bash
sherlock connections gmail fetch
```

Or watch continuously in the foreground:

```bash
sherlock connections gmail watch
```

The connection command records the current mailbox history as a baseline, so
existing messages are not replayed as new. The default watch interval is 30
seconds. Use `--interval SECONDS` to change it or `--json` on `fetch`/`watch` for
machine-readable output.

Check local connection status:

```bash
sherlock connections gmail status
```

OAuth refresh tokens and synchronization state are stored under
`~/.config/agent-sherlock/connections/gmail/`. On POSIX systems, Sherlock
enforces `0700` on that directory and `0600` on its files, and all updates are
atomic. Set `SHERLOCK_CONFIG_DIR` to override the configuration root; otherwise
`XDG_CONFIG_HOME` is honored when present.

To revoke access, remove Agent Sherlock from the third-party connections page
of your Google Account. Never commit either the downloaded OAuth JSON or
Sherlock's token file.

## Adding a command

Each subcommand lives in `src/agent_sherlock/commands/`. Copy `_template.py`,
implement your command, and register its `Command` instance in
`commands/__init__.py`'s `COMMANDS` list. A handler returns an `int` exit code
(`0` = success).
