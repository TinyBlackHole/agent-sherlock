from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from agent_sherlock.commands.base import Command

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
DEFAULT_POLL_INTERVAL_SECONDS = 30


def config_root() -> Path:
    override = os.environ.get("SHERLOCK_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    return Path("~/.config/agent-sherlock").expanduser()


def gmail_config_dir() -> Path:
    return config_root() / "connections" / "gmail"


def gmail_credentials_path() -> Path:
    return gmail_config_dir() / "credentials.json"


def gmail_token_path() -> Path:
    return gmail_config_dir() / "token.json"


def gmail_state_path() -> Path:
    return gmail_config_dir() / "state.json"


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        return {}
    return data


def save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, sort_keys=True)
        file.write("\n")


def configure(parser: argparse.ArgumentParser) -> None:
    parser.set_defaults(connections_parser=parser)
    providers = parser.add_subparsers(dest="provider", metavar="<provider>")

    gmail = providers.add_parser(
        "gmail",
        help="Connect and watch a Gmail account.",
        description="Connect and watch a Gmail account.",
    )
    gmail.set_defaults(gmail_parser=gmail)
    gmail_actions = gmail.add_subparsers(dest="action", metavar="<action>")

    connect = gmail_actions.add_parser(
        "connect",
        help="Authorize Agent Sherlock to read Gmail metadata.",
        description="Authorize Agent Sherlock to read Gmail metadata.",
    )
    connect.add_argument(
        "--credentials",
        required=True,
        help="Path to a Google OAuth Desktop client credentials JSON file.",
    )
    connect.set_defaults(connection_handler=run_gmail_connect)

    watch = gmail_actions.add_parser(
        "watch",
        help="Poll Gmail and touch ~/hello.txt for each new inbox email.",
        description="Poll Gmail and touch ~/hello.txt for each new inbox email.",
    )
    watch.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL_SECONDS,
        help=f"Seconds between Gmail polls. Default: {DEFAULT_POLL_INTERVAL_SECONDS}.",
    )
    watch.set_defaults(connection_handler=run_gmail_watch)


def run(args: argparse.Namespace) -> int:
    handler = getattr(args, "connection_handler", None)
    if handler is not None:
        return handler(args)

    parser = getattr(args, "gmail_parser", None) or getattr(
        args, "connections_parser", None
    )
    if parser is not None:
        parser.print_help()
    return 0


def run_gmail_connect(args: argparse.Namespace) -> int:
    source = Path(args.credentials).expanduser()
    if not source.is_file():
        print(f"Gmail credentials file not found: {source}")
        return 2

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print(
            "Missing Google OAuth dependency. Install the project dependencies, "
            "then run this command again."
        )
        return 1

    destination = gmail_credentials_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)

    flow = InstalledAppFlow.from_client_secrets_file(str(destination), GMAIL_SCOPES)
    credentials = flow.run_local_server(port=0)
    gmail_token_path().write_text(credentials.to_json(), encoding="utf-8")

    print(f"Gmail connected. Token saved to {gmail_token_path()}.")
    return 0


def build_gmail_service() -> tuple[Any | None, int]:
    credentials_file = gmail_credentials_path()
    token_file = gmail_token_path()
    if not credentials_file.exists() or not token_file.exists():
        print(
            "Gmail is not connected. Run "
            "`sherlock connections gmail connect --credentials PATH` first."
        )
        return None, 2

    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
    except ImportError:
        print(
            "Missing Google API dependency. Install the project dependencies, "
            "then run this command again."
        )
        return None, 1

    credentials = Credentials.from_authorized_user_file(str(token_file), GMAIL_SCOPES)
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
        token_file.write_text(credentials.to_json(), encoding="utf-8")

    return build("gmail", "v1", credentials=credentials), 0


def list_inbox_message_ids(service: Any) -> set[str]:
    ids: set[str] = set()
    page_token: str | None = None

    while True:
        request = (
            service.users()
            .messages()
            .list(
                userId="me",
                labelIds=["INBOX"],
                maxResults=500,
                pageToken=page_token,
            )
        )
        response = request.execute()
        for message in response.get("messages", []):
            message_id = message.get("id")
            if message_id:
                ids.add(message_id)

        page_token = response.get("nextPageToken")
        if not page_token:
            return ids


def touch_hello() -> int:
    result = subprocess.run(
        ["touch", os.path.expanduser("~/hello.txt")],
        check=False,
    )
    return result.returncode


def gmail_watch_once(service: Any, state_path: Path | None = None) -> int:
    state_file = state_path or gmail_state_path()
    state = load_json(state_file)
    current_ids = list_inbox_message_ids(service)

    if "seen_message_ids" not in state:
        save_json(state_file, {"seen_message_ids": sorted(current_ids)})
        print("Gmail baseline saved. Waiting for new inbox email.")
        return 0

    seen_ids = set(state.get("seen_message_ids", []))
    new_ids = current_ids - seen_ids
    for _message_id in sorted(new_ids):
        touch_hello()

    save_json(state_file, {"seen_message_ids": sorted(seen_ids | current_ids)})
    return len(new_ids)


def run_gmail_watch(args: argparse.Namespace) -> int:
    if args.interval <= 0:
        print("--interval must be greater than 0.")
        return 2

    service, exit_code = build_gmail_service()
    if service is None:
        return exit_code

    print("Watching Gmail. Press Ctrl+C to stop.")
    try:
        while True:
            try:
                triggered = gmail_watch_once(service)
                if triggered:
                    print(f"Triggered touch for {triggered} new Gmail message(s).")
            except Exception as exc:  # noqa: BLE001
                print(f"Gmail watch error: {exc}")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("Stopped Gmail watch.")
        return 0


COMMAND = Command(
    name="connections",
    help="Manage external service connections.",
    handler=run,
    configure=configure,
)
