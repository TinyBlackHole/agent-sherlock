# Agent Sherlock

A local message hub that securely reads incoming events from connected services
and delivers them to one private Telegram chat.

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

## Connect Telegram

Telegram is Sherlock's only output. Create a bot with
[@BotFather](https://t.me/BotFather), then run:

```bash
sherlock connections telegram connect
```

The token is requested with hidden terminal input. Sherlock validates the bot
and prints a one-time Telegram link. Open the link and press **Start** to
authorize that private chat.

For non-interactive setup, place the token in a private temporary file:

```bash
sherlock connections telegram connect --token-file /path/to/private-token
```

The one-time link remains required unless an existing private chat is selected
with `--chat-id`. Check the connection or send a test:

```bash
sherlock connections telegram status
sherlock connections telegram test
```

## Connect Gmail as an input

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

Sherlock requests only `gmail.readonly`. It can read incoming email content but
cannot modify or send mail. The downloaded OAuth client file is read during
connection and is not copied into Sherlock's configuration.

Versions that previously used `gmail.metadata` must reconnect once so Gmail can
grant the new read-only scope.

The full body of each newly discovered email is stored locally as plaintext in
SQLite until and after delivery. Sherlock protects that database with private
filesystem permissions, but does not application-encrypt its contents.

Check once for mail received since the previous check and deliver it to
Telegram:

```bash
sherlock connections gmail fetch
```

Or watch continuously in the foreground:

```bash
sherlock connections gmail watch
```

The Gmail connection records the current mailbox history as a baseline, so
existing messages are not replayed. The default watch interval is 30 seconds.
Use `--interval SECONDS` to change it or `--json` on `fetch`/`watch` for an
operational result containing counts only. Email content is delivered only to
Telegram, never printed by these commands.

Check local connection status:

```bash
sherlock connections gmail status
```

The status includes the number of messages in durable dead-letter storage so
permanent delivery failures remain visible.

OAuth refresh tokens and synchronization state are stored under
`~/.config/agent-sherlock/connections/`. The durable message inbox and delivery
state live in `~/.config/agent-sherlock/sherlock.db`. On POSIX systems, Sherlock
creates its managed directories with `0700`, enforces `0600` on private files,
and writes configuration atomically. An existing custom configuration root
keeps its original mode; private directories below it are still hardened. Set
`SHERLOCK_CONFIG_DIR` to override the configuration root; otherwise
`XDG_CONFIG_HOME` is honored.

Messages are inserted into SQLite before Gmail's history checkpoint advances.
Retryable or unclassified Telegram failures therefore remain pending without
losing the incoming email. A repeatedly failing error that Telegram explicitly
classifies as non-retryable is moved to durable dead-letter storage after three
attempts so it cannot block later messages. `watch` applies backoff while those
delivery attempts are pending. Provider event IDs prevent the same email from
being inserted twice.

To revoke access, remove Agent Sherlock from the third-party connections page
of your Google Account. Never commit either the downloaded OAuth JSON or
Sherlock's token files. Regenerate the Telegram bot token with BotFather if it is
ever exposed.

## Architecture

Every input connector converts its provider event into a common
`InboundMessage`. The pipeline persists and deduplicates that message before
acknowledging the provider checkpoint. A processor prepares the output, and the
single Telegram destination delivers it.

```text
Gmail / future inputs
        |
        v
InboundMessage -> SQLite inbox -> processor -> private Telegram chat
```

The current processor forwards a safe plain-text representation. An AI provider
can be inserted at that boundary later without coupling it to Gmail, Discord, or
Telegram.

## Adding a command

Each subcommand lives in `src/agent_sherlock/commands/`. Copy `_template.py`,
implement your command, and register its `Command` instance in
`commands/__init__.py`'s `COMMANDS` list. A handler returns an `int` exit code
(`0` = success).
