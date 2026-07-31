from __future__ import annotations

import json
from base64 import urlsafe_b64decode
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.message import Message
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from agent_sherlock.storage import (
    StorageError,
    atomic_write_json,
    config_root,
    read_json_object,
)

GMAIL_SCOPES = ("https://www.googleapis.com/auth/gmail.readonly",)
STATE_SCHEMA_VERSION = 1
MAX_CLIENT_SECRETS_BYTES = 1_000_000
MAX_MESSAGE_BODY_CHARACTERS = 100_000
# Budgets for hostile or simply enormous payloads. A message is untrusted input:
# it must never be able to exhaust memory or the interpreter's stack.
MAX_MESSAGE_PARTS = 1_000
MAX_MESSAGE_PART_DEPTH = 64
MAX_DECODED_BODY_CHARACTERS = 4 * MAX_MESSAGE_BODY_CHARACTERS
MAX_HISTORY_PAGES = 100
# Fetching happens before a batch is persisted, so keep the normal batch small
# enough that 100 maximum-sized bodies do not turn one poll into a memory spike.
MAX_HISTORY_MESSAGE_IDS = 100
GOOGLE_AUTH_URIS = {
    "https://accounts.google.com/o/oauth2/auth",
    "https://accounts.google.com/o/oauth2/v2/auth",
}
GOOGLE_OAUTH_EXCHANGE_URI = "https://oauth2.googleapis.com/token"
GMAIL_RETRYABLE_FORBIDDEN_REASONS = frozenset(
    {
        "rateLimitExceeded",
        "userRateLimitExceeded",
    }
)


class GmailError(RuntimeError):
    """Base class for expected Gmail integration failures."""


class GmailDependencyError(GmailError):
    """Raised when optional Google client libraries are unavailable."""


class GmailConfigurationError(GmailError):
    """Raised when local Gmail configuration is missing or invalid."""


class GmailAuthenticationError(GmailError):
    """Raised when Google authorization is missing, rejected, or expired."""


class GmailAPIError(GmailError):
    """Raised when Gmail returns an unexpected API failure."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        reasons: frozenset[str] = frozenset(),
    ):
        super().__init__(message)
        self.status = status
        self.reasons = reasons

    @property
    def retryable(self) -> bool:
        return (
            self.status is None
            or self.status in {408, 429}
            or self.status >= 500
            or (
                self.status == 403
                and not self.reasons.isdisjoint(GMAIL_RETRYABLE_FORBIDDEN_REASONS)
            )
        )


class GmailHistoryExpiredError(GmailAPIError):
    """Raised when Gmail can no longer serve an incremental history range."""


class GmailMessageUnavailableError(GmailAPIError):
    """Raised when a message disappeared before its metadata was fetched."""


@dataclass(frozen=True)
class GmailPaths:
    directory: Path

    @classmethod
    def default(cls) -> GmailPaths:
        return cls(config_root() / "connections" / "gmail")

    @property
    def token(self) -> Path:
        return self.directory / "token.json"

    @property
    def state(self) -> Path:
        return self.directory / "state.json"


@dataclass(frozen=True)
class GmailProfile:
    email_address: str
    history_id: str


@dataclass(frozen=True)
class GmailState:
    history_id: str
    email_address: str = ""
    schema_version: int = STATE_SCHEMA_VERSION

    def as_json(self) -> dict[str, Any]:
        return {
            "email_address": self.email_address,
            "history_id": self.history_id,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class GmailMessage:
    message_id: str
    thread_id: str
    sender: str
    subject: str
    date: str
    internal_date: int
    body: str = ""


@dataclass(frozen=True)
class GmailFetchResult:
    messages: tuple[GmailMessage, ...] = ()
    initialized: bool = False
    history_reset: bool = False


@dataclass(frozen=True)
class GmailFetchPlan:
    result: GmailFetchResult
    next_state: GmailState
    paths: GmailPaths


@dataclass(frozen=True)
class GmailStatus:
    connected: bool
    email_address: str = ""


def _decode_email_header(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    try:
        return str(make_header(decode_header(value)))
    except (LookupError, UnicodeError):
        return value


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignored_depth = 0
        self._text: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        _attributes: list[tuple[str, str | None]],
    ) -> None:
        if tag in {"script", "style"}:
            self._ignored_depth += 1
        elif self._ignored_depth == 0 and tag in {
            "br",
            "div",
            "li",
            "p",
            "table",
            "tr",
        }:
            self._text.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._ignored_depth:
            self._ignored_depth -= 1
        elif self._ignored_depth == 0 and tag in {"div", "li", "p", "table", "tr"}:
            self._text.append("\n")

    def handle_data(self, data: str) -> None:
        if self._ignored_depth == 0:
            self._text.append(data)

    def text(self) -> str:
        lines = (" ".join(line.split()) for line in "".join(self._text).splitlines())
        return "\n".join(line for line in lines if line)


def _part_header(part: dict[str, Any], name: str) -> str:
    headers = part.get("headers", [])
    if not isinstance(headers, list):
        return ""
    for header in headers:
        if not isinstance(header, dict):
            continue
        header_name = header.get("name")
        value = header.get("value")
        if (
            isinstance(header_name, str)
            and header_name.casefold() == name.casefold()
            and isinstance(value, str)
        ):
            return value
    return ""


def _decode_part_text(
    part: dict[str, Any],
    *,
    max_characters: int = MAX_DECODED_BODY_CHARACTERS,
) -> str:
    if max_characters <= 0:
        return ""
    body = part.get("body", {})
    if not isinstance(body, dict):
        return ""
    encoded = body.get("data")
    if not isinstance(encoded, str) or not encoded:
        return ""

    try:
        # Decode only a bounded prefix. The Gmail response already holds the
        # base64 string, but this avoids allocating another unbounded byte
        # buffer for a hostile single MIME part.
        max_decoded_bytes = max_characters * 4
        max_encoded_characters = ((max_decoded_bytes + 2) // 3) * 4
        encoded_prefix = encoded[:max_encoded_characters]
        padding = "=" * (-len(encoded_prefix) % 4)
        raw = urlsafe_b64decode(f"{encoded_prefix}{padding}")
    except (ValueError, TypeError):
        return ""

    content_type = _part_header(part, "Content-Type")
    message = Message()
    if content_type:
        message["content-type"] = content_type
    charset = message.get_content_charset() or "utf-8"
    try:
        return raw.decode(charset, errors="replace")[:max_characters]
    except LookupError:
        return raw.decode("utf-8", errors="replace")[:max_characters]


def _message_body(payload: dict[str, Any]) -> str:
    plain_text: list[str] = []
    html_text: list[str] = []
    collected_characters = 0
    visited_parts = 0

    # Walked iteratively on an explicit stack: a deeply nested MIME tree used to
    # recurse once per level and could exhaust the interpreter's stack.
    stack: list[tuple[dict[str, Any], int]] = [(payload, 0)]
    while stack:
        part, depth = stack.pop()
        visited_parts += 1
        if visited_parts > MAX_MESSAGE_PARTS:
            break

        filename = part.get("filename")
        if not (isinstance(filename, str) and filename):
            mime_type = part.get("mimeType")
            if mime_type in {"text/plain", "text/html"}:
                remaining_characters = (
                    MAX_DECODED_BODY_CHARACTERS - collected_characters
                )
                text = _decode_part_text(
                    part,
                    max_characters=remaining_characters,
                )
                if text:
                    collected_characters += len(text)
                    target = plain_text if mime_type == "text/plain" else html_text
                    target.append(text)
                    if collected_characters >= MAX_DECODED_BODY_CHARACTERS:
                        break

        if depth >= MAX_MESSAGE_PART_DEPTH:
            continue
        parts = part.get("parts", [])
        if isinstance(parts, list):
            available_slots = MAX_MESSAGE_PARTS - visited_parts - len(stack)
            if available_slots <= 0:
                continue
            # Reversed so the stack still yields parts in document order.
            for child in reversed(parts[:available_slots]):
                if isinstance(child, dict):
                    stack.append((child, depth + 1))

    text = "\n".join(plain_text).strip()
    if not text and html_text:
        extractor = _HTMLTextExtractor()
        try:
            extractor.feed("\n".join(html_text))
            extractor.close()
            text = extractor.text().strip()
        except (UnicodeError, ValueError):
            text = ""
    if len(text) > MAX_MESSAGE_BODY_CHARACTERS:
        return f"{text[: MAX_MESSAGE_BODY_CHARACTERS - 1]}…"
    return text


def _status_code(exception: Exception) -> int | None:
    response = getattr(exception, "resp", None)
    status = getattr(response, "status", None)
    return status if isinstance(status, int) else None


def _error_reasons(exception: Exception) -> frozenset[str]:
    content = getattr(exception, "content", None)
    if isinstance(content, bytes):
        try:
            content = content.decode("utf-8")
        except UnicodeDecodeError:
            return frozenset()
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except json.JSONDecodeError:
            return frozenset()
    if not isinstance(content, dict):
        return frozenset()

    error = content.get("error")
    if not isinstance(error, dict):
        return frozenset()

    reasons: set[str] = set()
    for key in ("errors", "details"):
        entries = error.get(key, [])
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            reason = entry.get("reason")
            if isinstance(reason, str) and reason:
                reasons.add(reason)
    return frozenset(reasons)


def _execute(request: Any, operation: str) -> dict[str, Any]:
    try:
        response = request.execute()
    except Exception as exc:  # third-party request boundary
        status = _status_code(exc)
        if operation == "read mailbox history" and status == 404:
            raise GmailHistoryExpiredError(
                "Gmail's incremental history expired; a new baseline is required."
            ) from exc
        if operation in {"fetch message", "fetch message metadata"} and status == 404:
            raise GmailMessageUnavailableError(
                "A Gmail message was removed before it could be fetched."
            ) from exc
        suffix = f" (HTTP {status})" if status is not None else ""
        raise GmailAPIError(
            f"Unable to {operation}{suffix}.",
            status=status,
            reasons=_error_reasons(exc),
        ) from exc

    if not isinstance(response, dict):
        raise GmailAPIError(
            f"Gmail returned an invalid response while trying to {operation}."
        )
    return response


class GmailMailbox:
    """Small, testable wrapper around the generated Gmail API client."""

    def __init__(self, service: Any):
        self._service = service

    def profile(self) -> GmailProfile:
        response = _execute(
            self._service.users().getProfile(userId="me"),
            "read the Gmail profile",
        )
        email_address = response.get("emailAddress")
        history_id = response.get("historyId")
        if not isinstance(email_address, str) or not email_address:
            raise GmailAPIError("Gmail returned a profile without an email address.")
        if not isinstance(history_id, str) or not history_id:
            raise GmailAPIError("Gmail returned a profile without a history ID.")
        return GmailProfile(email_address=email_address, history_id=history_id)

    def new_message_ids(self, start_history_id: str) -> tuple[list[str], str]:
        message_ids: list[str] = []
        seen_ids: set[str] = set()
        next_page_token: str | None = None
        latest_history_id = start_history_id
        # Resume point for a batch that hits a budget: the newest history record
        # actually processed, so the next poll continues instead of skipping.
        resume_history_id = ""
        seen_page_tokens: set[str] = set()
        pages_read = 0

        while True:
            arguments: dict[str, Any] = {
                "userId": "me",
                "startHistoryId": start_history_id,
                "historyTypes": ["messageAdded"],
                "labelId": "INBOX",
                "maxResults": 500,
            }
            if next_page_token is not None:
                arguments["pageToken"] = next_page_token

            request = self._service.users().history().list(**arguments)
            response = _execute(request, "read mailbox history")
            pages_read += 1

            response_history_id = response.get("historyId")
            if isinstance(response_history_id, str) and response_history_id:
                latest_history_id = response_history_id

            history_records = response.get("history", [])
            if not isinstance(history_records, list):
                raise GmailAPIError("Gmail returned invalid mailbox history.")

            budget_exhausted = False
            for record in history_records:
                if not isinstance(record, dict):
                    continue
                record_id = record.get("id")
                additions = record.get("messagesAdded", [])
                if not isinstance(additions, list):
                    continue
                record_message_ids: list[str] = []
                record_seen_ids: set[str] = set()
                for addition in additions:
                    if not isinstance(addition, dict):
                        continue
                    message = addition.get("message", {})
                    if not isinstance(message, dict):
                        continue
                    labels = message.get("labelIds", [])
                    if isinstance(labels, list) and labels and "INBOX" not in labels:
                        continue
                    message_id = message.get("id")
                    if (
                        isinstance(message_id, str)
                        and message_id
                        and message_id not in seen_ids
                        and message_id not in record_seen_ids
                    ):
                        record_message_ids.append(message_id)
                        record_seen_ids.add(message_id)

                # Never checkpoint a history record until all of its additions
                # are represented in this batch. Otherwise a limit reached in
                # the final API page would skip the unprocessed records.
                if (
                    message_ids
                    and len(message_ids) + len(record_message_ids)
                    > MAX_HISTORY_MESSAGE_IDS
                ):
                    budget_exhausted = True
                    break
                message_ids.extend(record_message_ids)
                seen_ids.update(record_seen_ids)
                if isinstance(record_id, str) and record_id.isdecimal():
                    resume_history_id = record_id
                if len(message_ids) >= MAX_HISTORY_MESSAGE_IDS:
                    budget_exhausted = True
                    break

            if budget_exhausted:
                return message_ids, resume_history_id or start_history_id
            page_token = response.get("nextPageToken")
            if not isinstance(page_token, str) or not page_token:
                return message_ids, latest_history_id
            if pages_read >= MAX_HISTORY_PAGES:
                # Stop before an unbounded backlog turns one poll into an
                # open-ended run, and check point where this batch stopped so
                # the remaining history is read on the next poll.
                return message_ids, resume_history_id or start_history_id
            if page_token in seen_page_tokens:
                raise GmailAPIError("Gmail returned a repeated mailbox-history page.")
            seen_page_tokens.add(page_token)
            next_page_token = page_token

    def message(self, message_id: str) -> GmailMessage:
        response = _execute(
            self._service.users()
            .messages()
            .get(
                userId="me",
                id=message_id,
                format="full",
            ),
            "fetch message",
        )
        payload = response.get("payload", {})
        headers = payload.get("headers", []) if isinstance(payload, dict) else []
        headers_by_name: dict[str, str] = {}
        if isinstance(headers, list):
            for header in headers:
                if not isinstance(header, dict):
                    continue
                name = header.get("name")
                value = header.get("value")
                if isinstance(name, str) and isinstance(value, str):
                    headers_by_name[name.casefold()] = _decode_email_header(value)

        internal_date_value = response.get("internalDate", "0")
        try:
            internal_date = int(internal_date_value)
        except (TypeError, ValueError):
            internal_date = 0

        thread_id = response.get("threadId")
        return GmailMessage(
            message_id=message_id,
            thread_id=thread_id if isinstance(thread_id, str) else "",
            sender=headers_by_name.get("from", ""),
            subject=headers_by_name.get("subject", ""),
            date=headers_by_name.get("date", ""),
            internal_date=internal_date,
            body=_message_body(payload) if isinstance(payload, dict) else "",
        )


def _load_state(paths: GmailPaths) -> GmailState | None:
    try:
        data = read_json_object(paths.state, missing_ok=True, private=True)
    except StorageError as exc:
        raise GmailConfigurationError(str(exc)) from exc
    if not data:
        return None

    # The prototype stored seen_message_ids. Treat it as an old schema and create a
    # fresh history baseline without replaying the existing inbox.
    if "seen_message_ids" in data and "history_id" not in data:
        return None

    schema_version = data.get("schema_version")
    history_id = data.get("history_id")
    email_address = data.get("email_address", "")
    if schema_version != STATE_SCHEMA_VERSION:
        raise GmailConfigurationError(
            "Unsupported Gmail state format. Reconnect Gmail to recreate it."
        )
    if not isinstance(history_id, str) or not history_id:
        raise GmailConfigurationError(
            "Invalid Gmail state. Reconnect Gmail to recreate it."
        )
    if not isinstance(email_address, str):
        raise GmailConfigurationError(
            "Invalid Gmail account state. Reconnect Gmail to recreate it."
        )
    return GmailState(history_id=history_id, email_address=email_address)


def _save_state(paths: GmailPaths, state: GmailState) -> None:
    try:
        atomic_write_json(paths.state, state.as_json())
    except StorageError as exc:
        raise GmailConfigurationError(str(exc)) from exc


def _read_client_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise GmailConfigurationError(
            f"Google OAuth credentials file not found: {path}"
        )
    try:
        if path.stat().st_size > MAX_CLIENT_SECRETS_BYTES:
            raise GmailConfigurationError(
                "Google OAuth credentials file is unexpectedly large."
            )
        with path.open(encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GmailConfigurationError(
            f"Cannot read Google OAuth credentials JSON: {path}"
        ) from exc

    if not isinstance(data, dict):
        raise GmailConfigurationError(
            "Google OAuth credentials must contain a JSON object."
        )
    installed = data.get("installed")
    if not isinstance(installed, dict):
        raise GmailConfigurationError(
            "Use OAuth credentials created with the Google 'Desktop app' client type."
        )

    client_id = installed.get("client_id")
    auth_uri = installed.get("auth_uri")
    token_uri = installed.get("token_uri")
    if not isinstance(client_id, str) or not client_id.endswith(
        ".apps.googleusercontent.com"
    ):
        raise GmailConfigurationError(
            "Google OAuth credentials have an invalid client ID."
        )
    if auth_uri not in GOOGLE_AUTH_URIS or token_uri != GOOGLE_OAUTH_EXCHANGE_URI:
        raise GmailConfigurationError(
            "Google OAuth credentials contain untrusted authorization endpoints."
        )
    return data


def _authorize(client_config: dict[str, Any]) -> Any:
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise GmailDependencyError(
            "Google OAuth support is not installed. Reinstall Agent Sherlock."
        ) from exc

    try:
        flow = InstalledAppFlow.from_client_config(client_config, list(GMAIL_SCOPES))
        return flow.run_local_server(
            host="127.0.0.1",
            port=0,
            access_type="offline",
            prompt="consent",
            authorization_prompt_message=(
                "Open this URL in your browser to connect Gmail:\n{url}"
            ),
            success_message=(
                "Gmail is connected to Agent Sherlock. You can close this window."
            ),
        )
    except Exception as exc:  # third-party OAuth boundary
        raise GmailAuthenticationError(
            "Gmail authorization did not complete. "
            "Your previous connection was unchanged."
        ) from exc


def _save_credentials(paths: GmailPaths, credentials: Any) -> None:
    try:
        token_data = json.loads(credentials.to_json())
        if not isinstance(token_data, dict):
            raise ValueError
        atomic_write_json(paths.token, token_data)
    except (
        AttributeError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        StorageError,
    ) as exc:
        raise GmailConfigurationError(
            "Could not securely save the Gmail authorization token."
        ) from exc


def _build_service(credentials: Any) -> Any:
    try:
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise GmailDependencyError(
            "Google API support is not installed. Reinstall Agent Sherlock."
        ) from exc

    try:
        return build(
            "gmail",
            "v1",
            credentials=credentials,
            cache_discovery=False,
        )
    except Exception as exc:  # third-party client construction boundary
        raise GmailAPIError("Could not initialize the Gmail API client.") from exc


def _load_credentials(paths: GmailPaths) -> Any:
    if not paths.token.exists():
        raise GmailAuthenticationError(
            "Gmail is not connected. Run `sherlock connections` to connect it."
        )

    try:
        token_data = read_json_object(paths.token, private=True)
    except StorageError as exc:
        raise GmailConfigurationError(str(exc)) from exc

    _validate_stored_scopes(token_data)

    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError as exc:
        raise GmailDependencyError(
            "Google authentication support is not installed. Reinstall Agent Sherlock."
        ) from exc

    try:
        credentials = Credentials.from_authorized_user_info(
            token_data,
            scopes=list(GMAIL_SCOPES),
        )
        if credentials.expired:
            if not credentials.refresh_token:
                raise GmailAuthenticationError(
                    "The Gmail session expired and cannot be refreshed. "
                    "Reconnect Gmail."
                )
            credentials.refresh(Request())
            _save_credentials(paths, credentials)
        if not credentials.valid:
            raise GmailAuthenticationError(
                "The Gmail session is invalid. Reconnect Gmail."
            )
        return credentials
    except GmailError:
        raise
    except Exception as exc:  # third-party credential boundary
        raise GmailAuthenticationError(
            "The Gmail session could not be refreshed. Reconnect Gmail."
        ) from exc


def _validate_stored_scopes(token_data: dict[str, Any]) -> None:
    granted_scopes = token_data.get("scopes", [])
    if isinstance(granted_scopes, str):
        granted_scopes = granted_scopes.split()
    if not isinstance(granted_scopes, list) or set(granted_scopes) != set(GMAIL_SCOPES):
        raise GmailAuthenticationError(
            "The stored Gmail authorization uses outdated permissions. Reconnect Gmail."
        )


def connect_gmail(
    client_secrets_path: Path,
    *,
    paths: GmailPaths | None = None,
) -> GmailProfile:
    selected_paths = paths or GmailPaths.default()
    client_config = _read_client_config(client_secrets_path.expanduser())
    credentials = _authorize(client_config)
    mailbox = GmailMailbox(_build_service(credentials))
    profile = mailbox.profile()

    # Nothing is changed locally until browser authorization and a Gmail API
    # profile request have both succeeded.
    _save_credentials(selected_paths, credentials)
    _save_state(
        selected_paths,
        GmailState(
            history_id=profile.history_id,
            email_address=profile.email_address,
        ),
    )
    return profile


def open_gmail_mailbox(*, paths: GmailPaths | None = None) -> GmailMailbox:
    selected_paths = paths or GmailPaths.default()
    return GmailMailbox(_build_service(_load_credentials(selected_paths)))


def fetch_new_gmail_messages(
    mailbox: GmailMailbox,
    *,
    paths: GmailPaths | None = None,
) -> GmailFetchResult:
    plan = prepare_gmail_fetch(mailbox, paths=paths)
    commit_gmail_fetch(plan)
    return plan.result


def prepare_gmail_fetch(
    mailbox: GmailMailbox,
    *,
    paths: GmailPaths | None = None,
) -> GmailFetchPlan:
    """Read an incremental batch without advancing the durable checkpoint."""
    selected_paths = paths or GmailPaths.default()
    state = _load_state(selected_paths)
    if state is None:
        profile = mailbox.profile()
        return GmailFetchPlan(
            result=GmailFetchResult(initialized=True),
            next_state=GmailState(
                history_id=profile.history_id,
                email_address=profile.email_address,
            ),
            paths=selected_paths,
        )

    try:
        message_ids, latest_history_id = mailbox.new_message_ids(state.history_id)
    except GmailHistoryExpiredError:
        profile = mailbox.profile()
        return GmailFetchPlan(
            result=GmailFetchResult(history_reset=True),
            next_state=GmailState(
                history_id=profile.history_id,
                email_address=profile.email_address,
            ),
            paths=selected_paths,
        )

    messages: list[GmailMessage] = []
    for message_id in message_ids:
        try:
            messages.append(mailbox.message(message_id))
        except GmailMessageUnavailableError:
            continue

    messages.sort(key=lambda message: (message.internal_date, message.message_id))
    return GmailFetchPlan(
        result=GmailFetchResult(messages=tuple(messages)),
        next_state=GmailState(
            history_id=latest_history_id,
            email_address=state.email_address,
        ),
        paths=selected_paths,
    )


def commit_gmail_fetch(plan: GmailFetchPlan) -> None:
    """Advance Gmail only after the caller has durably handled every message."""
    _save_state(plan.paths, plan.next_state)


def gmail_status(*, paths: GmailPaths | None = None) -> GmailStatus:
    selected_paths = paths or GmailPaths.default()
    if not selected_paths.token.exists():
        return GmailStatus(connected=False)
    try:
        token_data = read_json_object(selected_paths.token, private=True)
    except StorageError as exc:
        raise GmailConfigurationError(str(exc)) from exc
    _validate_stored_scopes(token_data)
    state = _load_state(selected_paths)
    return GmailStatus(
        connected=True,
        email_address=state.email_address if state is not None else "",
    )
