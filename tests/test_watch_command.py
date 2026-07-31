from agent_sherlock import input_settings
from agent_sherlock.cli import main
from agent_sherlock.commands import watch
from agent_sherlock.integrations.discord import DiscordCredentials, DiscordStatus
from agent_sherlock.integrations.gmail import GmailError, GmailStatus

TOKEN = "discord-bot-token-with-enough-characters"


def _discord_credentials():
    return DiscordCredentials(
        token=TOKEN,
        bot_id=42,
        bot_username="sherlock_bot",
        guild_id=8,
        channel_id=9,
        channel_name="alerts",
    )


def _prepare_connected_watch(monkeypatch, *, gmail=True, discord=True):
    credentials = _discord_credentials()
    monkeypatch.setattr(
        watch,
        "gmail_status",
        lambda: GmailStatus(
            connected=gmail,
            email_address="person@example.com" if gmail else "",
        ),
    )
    monkeypatch.setattr(
        watch,
        "discord_status",
        lambda: DiscordStatus(
            connected=discord,
            bot_username="sherlock_bot" if discord else "",
            guild_id=8 if discord else None,
            channel_id=9 if discord else None,
            channel_name="alerts" if discord else "",
        ),
    )
    monkeypatch.setattr(watch, "load_discord_credentials", lambda: credentials)
    monkeypatch.setattr(watch, "active_destination_name", lambda: "telegram")

    class Destination:
        name = "telegram"

        def send(self, _text):
            pass

    monkeypatch.setattr(
        watch.ActiveDestination,
        "open",
        classmethod(lambda cls, validator=None: Destination()),
    )

    class Repository:
        def initialize(self):
            pass

    monkeypatch.setattr(watch, "MessageRepository", Repository)
    monkeypatch.setattr(watch, "open_gmail_mailbox", lambda: object())


def test_watch_starts_every_connected_and_enabled_input(monkeypatch, capsys):
    _prepare_connected_watch(monkeypatch)
    started = []

    def watch_gmail(_connector, _pipeline, **kwargs):
        started.append(("gmail", kwargs["interval"], kwargs["stop_event"]))
        return 0

    def watch_discord(_connector, _pipeline, **kwargs):
        started.append(
            (
                "discord",
                kwargs["destination_name"],
                kwargs["stop_event"],
                kwargs["ready_message"],
            )
        )
        return 0

    monkeypatch.setattr(
        watch.connections_gmail,
        "watch_connected_gmail",
        watch_gmail,
    )
    monkeypatch.setattr(
        watch.connections_discord,
        "watch_connected_discord",
        watch_discord,
    )

    assert main(["watch", "--interval", "15"]) == 0

    assert {entry[0] for entry in started} == {"gmail", "discord"}
    assert next(entry[1] for entry in started if entry[0] == "gmail") == 15
    discord_start = next(entry for entry in started if entry[0] == "discord")
    assert discord_start[1] == "telegram"
    assert discord_start[3] == "✓ Discord: watching #alerts"
    printed = capsys.readouterr().out
    assert "Forwarding Gmail, Discord to telegram" in printed
    assert "✓ Gmail: watching every 15 seconds" in printed
    assert "✓ Discord: watching #alerts" not in printed


def test_watch_skips_a_connected_but_paused_input(monkeypatch, capsys):
    input_settings.set_input_enabled("gmail", False)
    _prepare_connected_watch(monkeypatch)
    started = []
    monkeypatch.setattr(
        watch.connections_gmail,
        "watch_connected_gmail",
        lambda *_args, **_kwargs: started.append("gmail") or 0,
    )
    monkeypatch.setattr(
        watch.connections_discord,
        "watch_connected_discord",
        lambda *_args, **_kwargs: started.append("discord") or 0,
    )

    assert main(["watch"]) == 0

    assert started == ["discord"]
    printed = capsys.readouterr().out
    assert "- Gmail: connected, paused" in printed
    assert "Forwarding Discord to telegram" in printed


def test_watch_reports_when_no_active_inputs_are_available(monkeypatch, capsys):
    _prepare_connected_watch(monkeypatch, gmail=False, discord=False)

    assert main(["watch"]) == 1

    printed = capsys.readouterr()
    assert "- Gmail: not connected" in printed.out
    assert "- Discord: not connected" in printed.out
    assert "no connected and enabled inputs" in printed.err


def test_one_failed_watcher_does_not_prevent_the_other_from_running(
    monkeypatch,
    capsys,
):
    _prepare_connected_watch(monkeypatch)
    started = []
    monkeypatch.setattr(
        watch.connections_gmail,
        "watch_connected_gmail",
        lambda *_args, **_kwargs: started.append("gmail") or 1,
    )
    monkeypatch.setattr(
        watch.connections_discord,
        "watch_connected_discord",
        lambda *_args, **_kwargs: started.append("discord") or 0,
    )

    assert main(["watch"]) == 1

    assert set(started) == {"gmail", "discord"}
    assert "All remaining input watchers have stopped" in capsys.readouterr().err


def test_watch_rejects_an_invalid_poll_interval_for_gmail(monkeypatch, capsys):
    _prepare_connected_watch(monkeypatch, discord=False)

    assert main(["watch", "--interval", "0"]) == 2
    assert "--interval must be" in capsys.readouterr().err


def test_watch_ignores_the_gmail_interval_when_only_discord_runs(
    monkeypatch,
    capsys,
):
    _prepare_connected_watch(monkeypatch, gmail=False)
    monkeypatch.setattr(
        watch.connections_discord,
        "watch_connected_discord",
        lambda *_args, **_kwargs: 0,
    )

    assert main(["watch", "--interval", "0"]) == 0
    assert "--interval must be" not in capsys.readouterr().err


def test_watch_reports_discovery_failure_instead_of_connection_guidance(
    monkeypatch,
    capsys,
):
    _prepare_connected_watch(monkeypatch, gmail=False, discord=False)
    monkeypatch.setattr(
        watch,
        "gmail_status",
        lambda: (_ for _ in ()).throw(GmailError("cannot read Gmail settings")),
    )

    assert main(["watch"]) == 1

    output = capsys.readouterr()
    assert "connection discovery failed" in output.err
    assert "Connect an input or enable it" not in output.err


def test_watch_rejects_invalid_local_ai_configuration(monkeypatch, capsys):
    _prepare_connected_watch(monkeypatch, discord=False)
    monkeypatch.setattr(
        watch,
        "open_message_processor",
        lambda: (_ for _ in ()).throw(
            watch.AIConfigurationError("configured Ollama model is missing")
        ),
    )

    assert main(["watch"]) == 1

    assert "configured Ollama model is missing" in capsys.readouterr().err


def test_second_interrupt_forces_exit_from_daemon_workers(monkeypatch, capsys):
    _prepare_connected_watch(monkeypatch)
    created = []
    interrupts = iter([KeyboardInterrupt(), KeyboardInterrupt()])

    class Thread:
        def __init__(self, *, target, args, name, daemon):
            self.target = target
            self.args = args
            self.name = name
            self.daemon = daemon
            created.append(self)

        def start(self):
            pass

        def is_alive(self):
            return True

        def join(self, *, timeout):
            assert timeout == 0.2
            raise next(interrupts)

    monkeypatch.setattr(watch.threading, "Thread", Thread)

    assert main(["watch"]) == 130

    assert created
    assert all(thread.daemon for thread in created)
    assert "forcing Sherlock watch to exit" in capsys.readouterr().err
