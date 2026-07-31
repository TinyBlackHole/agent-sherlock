# Agent Sherlock

A local message hub that securely reads incoming events from connected services
and delivers them to one selected private Telegram or Discord destination.

Source: https://github.com/TinyBlackHole/agent-sherlock

## Install

Install Agent Sherlock with:

```bash
curl -fsSL https://sherlock.tinyblackhole.com/install.sh | sh
```

The installer pins a protected, versioned release tag rather than tracking the
default branch, so two installations of the same version get the same code. Set
`SHERLOCK_VERSION` to install a different tag. Read the script before piping it
into a shell, as with any `curl | sh` installer.

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

Telegram is one of Sherlock's two outputs, and the default. Create a bot with
[@BotFather](https://t.me/BotFather), then run:

```bash
sherlock output telegram connect
```

The token is requested with hidden terminal input. Sherlock validates the bot
and prints a one-time Telegram link. Open the link and press **Start** to
authorize that private chat.

For non-interactive setup, place the token in a private temporary file:

```bash
sherlock output telegram connect --token-file /path/to/private-token
```

Content that exceeds Telegram's message limit is delivered as a single upload
with a preview caption and the complete text attached. Sending it as several
consecutive messages would make a mid-delivery failure re-send the parts that
already arrived.

The one-time link remains required unless an existing private chat is selected
with `--chat-id`. Check the connection or send a test:

```bash
sherlock output telegram status
sherlock output telegram test
```

## Connect Gmail as an input

Before the first connection:

1. Create or select a project in the
   [Google Cloud console](https://console.cloud.google.com/).
2. Enable the Gmail API.
3. Configure the OAuth consent screen. If the app is in testing mode, add your
   Gmail address as a test user.
4. Create an OAuth client with the **Desktop app** application type and download
   its JSON file. Keep it outside the project and, on POSIX systems, restrict it
   with `chmod 600 /path/to/credentials.json`.

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

The normalized body of each newly discovered email (up to 100 000 characters)
is stored locally as plaintext in SQLite until and after delivery. Sherlock
protects that database with private filesystem permissions, but does not
application-encrypt its contents. Delivered messages are deleted once they fall
outside the retention window — see [Local message
retention](#local-message-retention).

Check once for mail received since the previous check and deliver it to the
selected output:

```bash
sherlock connections gmail fetch
```

To watch only Gmail for diagnosis, run:

```bash
sherlock connections gmail watch
```

The Gmail connection records the current mailbox history as a baseline, so
existing messages are not replayed. The default watch interval is 30 seconds.
Use `--interval SECONDS` to change it or `--json` on `fetch`/the individual
Gmail watcher for an operational result containing counts only. Email content
is delivered only to the selected output, never printed by these commands.

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
Retryable or unclassified destination failures therefore remain pending without
losing the incoming email. A repeatedly failing error that the selected output
explicitly classifies as non-retryable is moved to durable dead-letter storage
after three attempts so it cannot block later messages. `watch` applies backoff
while those delivery attempts are pending. Provider event IDs prevent the same
email from being inserted twice.

To revoke access, remove Agent Sherlock from the third-party connections page
of your Google Account. Never commit either the downloaded OAuth JSON or
Sherlock's token files. Regenerate any Telegram bot token or Discord webhook
that is ever exposed.

## Connect Discord as an input

Sherlock can monitor one Discord server text channel in real time:

1. Create an application in the
   [Discord Developer Portal](https://discord.com/developers/applications) and
   add a bot.
2. On the bot settings page, enable **Message Content Intent**.
3. Install the bot in the server with permission to view the selected channel.
4. In Discord, enable Developer Mode and copy the channel ID.
5. Put the bot token in a private temporary file and connect it:

```bash
sherlock connections discord connect \
  --token-file /path/to/private-token \
  --channel-id 123456789012345678
```

The token is requested with hidden terminal input when `--token-file` is
omitted from an interactive terminal. Sherlock validates the bot and channel
before saving the connection. Check its local status with:

```bash
sherlock connections discord status
```

To run only the Discord Gateway connection for diagnosis:

```bash
sherlock connections discord watch
```

Only messages created while `watch` is connected are received; existing channel
history is not replayed. Text, attachment links, stickers, and basic embed
content are normalized into the durable inbox before delivery. Provider message
IDs make replay after a Gateway reconnect idempotent.

Gateway events arrive on a bounded queue drained by a single consumer, so a slow
or failing output cannot let callback tasks pile up without limit. If that
buffer ever fills, Sherlock stops loudly instead of silently dropping events.

Because Discord never replays an event Sherlock already received, a failure to
*store* one stops the watch with a non-zero exit code: the message is gone, and
continuing would hide that. Resolve the storage error and start the watch again.
A failure to *deliver* is different — the message is already durable, so the
watch stays connected and retries with exponential backoff instead of hammering
a down output once per incoming message.

The bot token is stored under
`~/.config/agent-sherlock/connections/discord/` with the same private-file
protections as the other connections. Never commit the token, and reset it in
the Developer Portal if it is exposed.

## Watch every active input

The normal way to run Sherlock is:

```bash
sherlock watch
```

Sherlock automatically discovers Gmail and Discord connections that are valid
locally and starts all of them in one foreground process. A connection is
enabled automatically when it is created. Inputs that are not connected are
skipped, and one input failing does not stop the others.

Pause an input without deleting its credentials, then enable it again later:

```bash
sherlock connections gmail disable
sherlock connections gmail enable
sherlock connections discord disable
sherlock connections discord enable
```

Connection status shows whether automatic watching is enabled or paused.
`sherlock watch --interval SECONDS` changes the polling interval for Gmail;
Discord continues to receive Gateway events in real time. Press **Ctrl+C** to
stop all inputs together. If a provider is stuck while shutting down, press
**Ctrl+C** again to force the foreground process to exit.

All workers share one durable inbox and one serialized delivery path. This
allows inputs to receive concurrently without racing to deliver the same queued
message. Queued messages are also claimed atomically in the database under a
lease, so two Sherlock processes cannot select the same unexpired work
concurrently; if one crashes mid-delivery, another picks its work up once the
lease expires. The selected output is still resolved for each delivery, so
switching between Telegram and Discord does not require restarting `sherlock
watch`.

## Delivery guarantees

Delivery is **at least once**, not exactly once. A message is stored before it
is sent and marked delivered only after the destination accepts it. This keeps
destination failures from discarding queued work, but a failure in the window
between the destination accepting a message and Sherlock recording that fact
results in a repeat on the next attempt. Each delivery is a single API request,
which keeps a retry from re-sending part of a message that already arrived.

Normalized message bodies are stored up to each connector's documented safety
limit, then delivered up to a per-message character budget (3 000 by default).
When a stored body does not fit, the delivered text ends with an explicit note
stating how many characters were left out, the operational result counts the
truncation, and the complete stored body stays in the local inbox.
Raise the budget with `SHERLOCK_MAX_DELIVERY_BODY_CHARACTERS` (200–100 000).

## Local message retention

Stored messages do not accumulate forever. Delivered messages are deleted after
30 days, dead-letter messages after 90, and the inbox is trimmed to 50 000 rows;
only already-handled messages are ever removed, so pending work is never dropped
to make room. Retention runs automatically at most once an hour from the
delivery path, and on demand:

```bash
sherlock purge --status          # report the inbox size, delete nothing
sherlock purge                   # apply the default retention policy
sherlock purge --days 7          # keep delivered messages for 7 days
sherlock purge --all             # drop every delivered and dead-letter message
```

`--max-messages` changes the size cap (`0` disables it), and
`--dead-letter-days` the dead-letter window.

## Choose where Sherlock delivers

Every input is forwarded to exactly one destination. Telegram is the default;
Discord delivers through a channel webhook.

```bash
sherlock output status
```

To deliver into Discord, create the webhook first. In the target channel open
**Channel Settings -> Integrations -> Webhooks -> New Webhook**, copy its URL,
then:

```bash
sherlock output discord connect --webhook-url-file /path/to/private-url
sherlock output use discord
sherlock output test
```

The URL is requested with hidden terminal input when `--webhook-url-file` is
omitted from an interactive terminal. Sherlock validates it against Discord and
records the channel before saving. Switch back at any time with
`sherlock output use telegram`. Running Gmail and Discord watchers observe the
new selection on their next delivery; they do not need to be restarted.

A webhook needs no bot or privileged intents. The person creating it needs
permission to manage webhooks in the channel. Its URL is a secret: anyone
holding it can post to that channel. It is stored under
`~/.config/agent-sherlock/connections/discord-webhook/` with the same
private-file protections as the other connections. Delete the webhook in
Discord if it is ever exposed.

Forwarded content is posted with mentions and link previews suppressed, so an
untrusted message body cannot ping a role or `@everyone`. Content that exceeds
Discord's message limit is delivered in one post with a preview and the complete
text attached, which prevents partial posts from being duplicated on retry.

If the Discord input watches the same channel the Discord output posts into,
both `sherlock watch` and `sherlock connections discord watch` refuse to start
that pairing: every delivered message would be read back as new input and
forwarded again.

## Architecture

Every input connector converts its provider event into a common
`InboundMessage`. The pipeline persists and deduplicates that message before
acknowledging the provider checkpoint. A processor prepares the output, and the
selected destination delivers it.

```text
Gmail / Discord / future inputs
        |
        v
concurrent watchers -> InboundMessage -> SQLite inbox
                                           |
                                           v
                      serialized delivery -> Telegram chat or Discord channel
```

The active destination is stored in
`~/.config/agent-sherlock/destination.json` and resolved through the
`MessageDestination` protocol, so inputs never know which one is configured.
Paused-input settings are stored in
`~/.config/agent-sherlock/inputs.json`; connected inputs are enabled by default.

The current processor forwards a safe plain-text representation. An AI provider
can be inserted at that boundary later without coupling it to Gmail, Discord, or
Telegram.

## Adding a command

Each subcommand lives in `src/agent_sherlock/commands/`. Copy `_template.py`,
implement your command, and register its `Command` instance in
`commands/__init__.py`'s `COMMANDS` list. A handler returns an `int` exit code
(`0` = success).
