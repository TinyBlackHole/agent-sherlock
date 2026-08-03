from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 90.0
STATUS_REQUEST_TIMEOUT_SECONDS = 5.0
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_ERROR_BYTES = 4_096
MAX_SUPPLEMENT_CHARACTERS = 12_000

_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "important": {
            "type": "boolean",
            "description": "Whether the configured importance criteria match.",
        },
        "supplement": {
            "type": "string",
            "description": "The result of applying the user's instruction.",
        },
    },
    "required": ["important", "supplement"],
    "additionalProperties": False,
}


class OllamaError(RuntimeError):
    """Base class for safe, user-visible Ollama failures."""

    retryable = False


class OllamaConfigurationError(OllamaError):
    """Raised when the local Ollama endpoint or model is invalid."""


class OllamaUnavailableError(OllamaError):
    """Raised when a local Ollama request may succeed if retried."""

    retryable = True


class OllamaResponseError(OllamaError):
    """Raised when Ollama returns an invalid or unsafe response."""


@dataclass(frozen=True, slots=True)
class OllamaModel:
    name: str
    digest: str = ""
    size: int = 0


@dataclass(frozen=True, slots=True)
class OllamaChatResult:
    supplement: str
    model: str
    important: bool = False


class _NoRedirectHandler(HTTPRedirectHandler):
    """Keep a loopback request from being redirected to a remote host."""

    def redirect_request(
        self,
        _request: Request,
        _file_pointer: Any,
        _code: int,
        _message: str,
        _headers: Any,
        _new_url: str,
    ) -> None:
        return None


def normalize_local_base_url(value: str) -> str:
    """Validate an Ollama URL without turning Sherlock into an SSRF client."""
    candidate = value.strip()
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError as exc:
        raise OllamaConfigurationError("The Ollama URL is invalid.") from exc

    if parsed.scheme != "http":
        raise OllamaConfigurationError(
            "Ollama must use a local http:// URL; remote or TLS endpoints are "
            "not supported."
        )
    if parsed.username or parsed.password:
        raise OllamaConfigurationError("The Ollama URL must not contain credentials.")
    if parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise OllamaConfigurationError(
            "The Ollama URL must contain only a local host and optional port."
        )

    hostname = (parsed.hostname or "").casefold()
    if hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise OllamaConfigurationError(
            "The Ollama URL must point to localhost, 127.0.0.1, or ::1."
        )
    if port is not None and not 1 <= port <= 65_535:
        raise OllamaConfigurationError("The Ollama port is invalid.")

    netloc = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None:
        netloc = f"{netloc}:{port}"
    return urlunsplit(("http", netloc, "", "", ""))


class OllamaClient:
    """Small native client for Ollama's local HTTP API."""

    def __init__(
        self,
        base_url: str = DEFAULT_OLLAMA_URL,
        *,
        timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        opener: Callable[..., Any] | None = None,
    ):
        self.base_url = normalize_local_base_url(base_url)
        self.timeout = timeout
        self._opener = opener or build_opener(_NoRedirectHandler()).open

    def list_models(self) -> tuple[OllamaModel, ...]:
        payload = self._request("GET", "/api/tags")
        models = payload.get("models")
        if not isinstance(models, list):
            raise OllamaResponseError(
                "Ollama returned an invalid installed-model list."
            )

        parsed_models: list[OllamaModel] = []
        for item in models:
            if not isinstance(item, dict):
                raise OllamaResponseError(
                    "Ollama returned an invalid installed-model entry."
                )
            name = item.get("name") or item.get("model")
            digest = item.get("digest", "")
            size = item.get("size", 0)
            if (
                not isinstance(name, str)
                or not name
                or not isinstance(digest, str)
                or isinstance(size, bool)
                or not isinstance(size, int)
            ):
                raise OllamaResponseError(
                    "Ollama returned an invalid installed-model entry."
                )
            parsed_models.append(
                OllamaModel(
                    name=_safe_text(name, limit=200),
                    digest=_safe_text(digest, limit=200),
                    size=max(size, 0),
                )
            )
        return tuple(parsed_models)

    def chat(
        self,
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
        keep_alive: str,
        temperature: float,
        max_output_tokens: int,
    ) -> OllamaChatResult:
        payload = self._request(
            "POST",
            "/api/chat",
            {
                "model": model,
                "stream": False,
                "keep_alive": keep_alive,
                "format": _OUTPUT_SCHEMA,
                "options": {
                    "temperature": temperature,
                    "num_predict": max_output_tokens,
                },
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            },
        )
        message = payload.get("message")
        response_model = payload.get("model", model)
        if not isinstance(message, dict) or not isinstance(response_model, str):
            raise OllamaResponseError("Ollama returned an invalid chat response.")
        content = message.get("content")
        if not isinstance(content, str):
            raise OllamaResponseError("Ollama returned an invalid chat response.")
        try:
            structured = json.loads(content)
        except json.JSONDecodeError as exc:
            raise OllamaResponseError(
                "Ollama did not return the required structured result."
            ) from exc
        if (
            not isinstance(structured, dict)
            or set(structured) != {"important", "supplement"}
            or type(structured.get("important")) is not bool
            or not isinstance(structured.get("supplement"), str)
        ):
            raise OllamaResponseError(
                "Ollama did not return the required structured result."
            )
        supplement = _safe_text(
            structured["supplement"],
            limit=MAX_SUPPLEMENT_CHARACTERS,
        ).strip()
        if not supplement:
            raise OllamaResponseError("Ollama returned an empty result.")
        return OllamaChatResult(
            supplement=supplement,
            model=_safe_text(response_model, limit=200),
            important=structured["important"],
        )

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(
                body,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(
            f"{self.base_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            response = self._opener(request, timeout=self.timeout)
            with response:
                content_length = response.headers.get("Content-Length")
                if content_length is not None:
                    try:
                        declared_size = int(content_length)
                    except ValueError as exc:
                        raise OllamaResponseError(
                            "Ollama returned an invalid response size."
                        ) from exc
                    if declared_size > MAX_RESPONSE_BYTES:
                        raise OllamaResponseError(
                            "Ollama returned a response that is too large."
                        )
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            detail = _http_error_detail(exc)
            message = f"Ollama request failed with HTTP {exc.code}"
            if detail:
                message = f"{message}: {detail}"
            if exc.code == 429 or exc.code >= 500:
                raise OllamaUnavailableError(message) from exc
            raise OllamaConfigurationError(message) from exc
        except (TimeoutError, URLError, OSError) as exc:
            raise OllamaUnavailableError(
                f"Cannot reach local Ollama at {self.base_url}."
            ) from exc

        if len(raw) > MAX_RESPONSE_BYTES:
            raise OllamaResponseError("Ollama returned a response that is too large.")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OllamaResponseError("Ollama returned invalid JSON.") from exc
        if not isinstance(payload, dict):
            raise OllamaResponseError("Ollama returned an invalid JSON response.")
        return payload


def _http_error_detail(error: HTTPError) -> str:
    try:
        raw = error.read(MAX_ERROR_BYTES + 1)[:MAX_ERROR_BYTES]
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict) or not isinstance(payload.get("error"), str):
        return ""
    return _safe_text(payload["error"], limit=500)


def _safe_text(value: str, *, limit: int) -> str:
    cleaned = "".join(
        character if character in {"\n", "\t"} or character.isprintable() else " "
        for character in value
    )
    return cleaned[:limit]
