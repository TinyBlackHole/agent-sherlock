from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from agent_sherlock.ai import (
    MAX_PROMPT_CHARACTERS,
    SYSTEM_PROMPT_VERSION,
    AIConfigurationError,
    OllamaMessageProcessor,
    configured_model,
    load_ai_config,
    save_ai_config,
    select_model,
)
from agent_sherlock.commands.base import Command
from agent_sherlock.commands.connections_shared import print_error, terminal_safe
from agent_sherlock.domain import InboundMessage
from agent_sherlock.integrations.ollama import (
    DEFAULT_OLLAMA_URL,
    STATUS_REQUEST_TIMEOUT_SECONDS,
    OllamaClient,
    OllamaError,
    OllamaModel,
)


def configure(parser: argparse.ArgumentParser) -> None:
    parser.set_defaults(ai_parser=parser)
    actions = parser.add_subparsers(dest="action", metavar="<action>")

    connect = actions.add_parser(
        "connect",
        help="Connect Sherlock to the local Ollama service.",
        description="Validate a local Ollama model and enable AI processing.",
    )
    connect.add_argument("provider", nargs="?", default="ollama")
    connect.add_argument("--model", help="Installed Ollama model name.")
    connect.add_argument(
        "--base-url",
        default=DEFAULT_OLLAMA_URL,
        help=f"Local Ollama URL. Default: {DEFAULT_OLLAMA_URL}.",
    )
    connect.set_defaults(ai_handler=run_connect)

    models = actions.add_parser(
        "models",
        help="List models installed in local Ollama.",
    )
    models.add_argument(
        "--base-url",
        help="Override the configured local Ollama URL for this lookup.",
    )
    models.set_defaults(ai_handler=run_models)

    model = actions.add_parser(
        "model",
        help="Select the Ollama model used for new messages.",
    )
    model_actions = model.add_subparsers(dest="model_action", metavar="<action>")
    model_set = model_actions.add_parser("set", help="Select an installed model.")
    model_set.add_argument("name", nargs="?")
    model_set.set_defaults(ai_handler=run_model_set)
    model.set_defaults(ai_parser=model)

    prompt = actions.add_parser(
        "prompt",
        help="Show or change the user's permanent AI instruction.",
    )
    prompt_actions = prompt.add_subparsers(dest="prompt_action", metavar="<action>")
    prompt_show = prompt_actions.add_parser(
        "show",
        help="Show the current instruction.",
    )
    prompt_show.set_defaults(ai_handler=run_prompt_show)
    prompt_set = prompt_actions.add_parser("set", help="Change the instruction.")
    prompt_set.add_argument(
        "text",
        nargs="*",
        help="Instruction text. Quote it when it contains spaces.",
    )
    prompt_set.add_argument(
        "--file",
        help="Read the instruction from a UTF-8 text file.",
    )
    prompt_set.set_defaults(ai_handler=run_prompt_set)
    prompt.set_defaults(ai_parser=prompt)

    mode = actions.add_parser(
        "mode",
        help="Choose whether AI augments or replaces the original message.",
    )
    mode.add_argument("value", nargs="?", metavar="{augment,replace}")
    mode.set_defaults(ai_handler=run_mode)

    enable = actions.add_parser(
        "enable",
        help="Enable local AI processing after validating its model.",
    )
    enable.set_defaults(ai_handler=run_enable)

    disable = actions.add_parser(
        "disable",
        help="Forward messages literally without deleting AI settings.",
    )
    disable.set_defaults(ai_handler=run_disable)

    status = actions.add_parser(
        "status",
        help="Show AI settings and verify local Ollama.",
    )
    status.set_defaults(ai_handler=run_status)

    test = actions.add_parser(
        "test",
        help="Apply the configured instruction to a synthetic local message.",
    )
    test.set_defaults(ai_handler=run_test)


def run(args: argparse.Namespace) -> int:
    handler = getattr(args, "ai_handler", None)
    if handler is not None:
        return handler(args)
    parser = getattr(args, "ai_parser", None)
    if parser is not None:
        parser.print_help()
    return 0


def run_connect(args: argparse.Namespace) -> int:
    if getattr(args, "provider", "ollama") != "ollama":
        print("Error: only the local Ollama provider is supported.", file=sys.stderr)
        return 2
    model_name = (getattr(args, "model", None) or "").strip()
    if not model_name:
        print(
            "Error: provide --model with a name from `sherlock ai models`.",
            file=sys.stderr,
        )
        return 2
    try:
        current = load_ai_config()
        candidate = replace(
            current,
            provider="ollama",
            base_url=getattr(args, "base_url", DEFAULT_OLLAMA_URL),
            model=model_name,
            model_digest="",
            enabled=False,
        )
        client = OllamaClient(candidate.base_url)
        model = _find_model(client.list_models(), model_name)
        save_ai_config(replace(select_model(candidate, model), enabled=True))
    except (AIConfigurationError, OllamaError) as exc:
        print_error(exc)
        return 1
    print(f"Local AI enabled with Ollama model {terminal_safe(model.name)}.")
    print('Change the instruction with `sherlock ai prompt set "..."`.')
    return 0


def run_models(args: argparse.Namespace) -> int:
    try:
        config = load_ai_config()
        base_url = getattr(args, "base_url", None) or config.base_url
        models = OllamaClient(
            base_url,
            timeout=STATUS_REQUEST_TIMEOUT_SECONDS,
        ).list_models()
    except (AIConfigurationError, OllamaError) as exc:
        print_error(exc)
        return 1
    if not models:
        print("No Ollama models are installed.")
        print("Install one with `ollama pull <model>`.")
        return 0
    print("Installed Ollama models:")
    for model in models:
        selected = " (selected)" if model.name == config.model else ""
        print(f"  {terminal_safe(model.name)}{selected}")
    return 0


def run_model_set(args: argparse.Namespace) -> int:
    model_name = (getattr(args, "name", None) or "").strip()
    if not model_name:
        print(
            "Error: provide a model name from `sherlock ai models`.",
            file=sys.stderr,
        )
        return 2
    try:
        config = load_ai_config()
        client = OllamaClient(config.base_url)
        model = _find_model(client.list_models(), model_name)
        save_ai_config(select_model(config, model))
    except (AIConfigurationError, OllamaError) as exc:
        print_error(exc)
        return 1
    print(
        f"New, unprocessed messages will use Ollama model {terminal_safe(model.name)}."
    )
    return 0


def run_prompt_show(_args: argparse.Namespace) -> int:
    try:
        config = load_ai_config()
    except AIConfigurationError as exc:
        print_error(exc)
        return 1
    print(config.prompt)
    return 0


def run_prompt_set(args: argparse.Namespace) -> int:
    words = getattr(args, "text", [])
    prompt_file = getattr(args, "file", None)
    if words and prompt_file:
        print("Error: provide instruction text or --file, not both.", file=sys.stderr)
        return 2
    if prompt_file:
        text = _read_prompt_file(Path(prompt_file).expanduser())
        if text is None:
            return 2
    else:
        text = " ".join(words).strip()
    if not text:
        print("Error: the AI instruction must not be empty.", file=sys.stderr)
        return 2
    try:
        config = load_ai_config()
        save_ai_config(replace(config, prompt=text))
    except AIConfigurationError as exc:
        print_error(exc)
        return 2
    print("AI instruction updated for new, unprocessed messages.")
    return 0


def run_mode(args: argparse.Namespace) -> int:
    value = (getattr(args, "value", None) or "").strip()
    if not value:
        try:
            print(f"AI delivery mode: {load_ai_config().mode}")
        except AIConfigurationError as exc:
            print_error(exc)
            return 1
        return 0
    if value not in {"augment", "replace"}:
        print("Error: AI mode must be augment or replace.", file=sys.stderr)
        return 2
    try:
        config = load_ai_config()
        save_ai_config(replace(config, mode=value))
    except AIConfigurationError as exc:
        print_error(exc)
        return 1
    description = (
        "the original message followed by the AI result"
        if value == "augment"
        else "only the AI result"
    )
    print(f"New, unprocessed messages will deliver {description}.")
    return 0


def run_enable(_args: argparse.Namespace) -> int:
    try:
        config = load_ai_config()
        model = configured_model(config)
        save_ai_config(replace(select_model(config, model), enabled=True))
    except AIConfigurationError as exc:
        print_error(exc)
        return 1
    print(f"Local AI enabled with {terminal_safe(model.name)}.")
    return 0


def run_disable(_args: argparse.Namespace) -> int:
    try:
        config = load_ai_config()
        save_ai_config(replace(config, enabled=False))
    except AIConfigurationError as exc:
        print_error(exc)
        return 1
    print("Local AI disabled. New messages will be forwarded literally.")
    return 0


def run_status(_args: argparse.Namespace) -> int:
    try:
        config = load_ai_config()
    except AIConfigurationError as exc:
        print_error(exc)
        return 1
    print(f"Local AI: {'enabled' if config.enabled else 'disabled'}")
    print("Provider: Ollama (local only)")
    print(f"Endpoint: {config.base_url}")
    print(f"Model: {terminal_safe(config.model) if config.model else '(not selected)'}")
    print(f"Delivery mode: {config.mode}")
    print(f"Permanent Agent Sherlock context: built in (v{SYSTEM_PROMPT_VERSION})")
    print("User instruction:")
    print(f"  {config.prompt.replace(chr(10), chr(10) + '  ')}")
    if not config.model:
        return 0 if not config.enabled else 1
    try:
        model = configured_model(
            config,
            client=OllamaClient(
                config.base_url,
                timeout=STATUS_REQUEST_TIMEOUT_SECONDS,
            ),
        )
    except AIConfigurationError as exc:
        print(f"Ollama status: unavailable ({exc})")
        return 1
    digest_changed = bool(
        config.model_digest and model.digest and config.model_digest != model.digest
    )
    suffix = "; model contents changed since selection" if digest_changed else ""
    print(f"Ollama status: ready{suffix}")
    return 0


def run_test(_args: argparse.Namespace) -> int:
    try:
        config = load_ai_config()
        model = configured_model(config)
        test_config = replace(
            select_model(config, model),
            enabled=True,
            mode="replace",
        )
        processor = OllamaMessageProcessor(test_config)
        result = processor.process(
            InboundMessage(
                source="test",
                account_id="local",
                external_id="ai-test",
                conversation_id="ai-test",
                sender="cliente@example.com",
                subject="Cambio de horario",
                body=(
                    "La reunión de mañana se movió de las 10:00 a las 11:30. "
                    "Confirma por favor que recibiste el cambio."
                ),
                received_at=datetime.now(UTC),
            )
        )
    except (AIConfigurationError, OllamaError) as exc:
        print_error(exc)
        return 1
    print("Local AI test result:")
    print(result.text)
    return 0


def _find_model(models: tuple[OllamaModel, ...], name: str) -> OllamaModel:
    for model in models:
        if model.name == name:
            return model
    raise AIConfigurationError(
        f"Ollama model {name!r} is not installed. Run `sherlock ai models`."
    )


def _read_prompt_file(path: Path) -> str | None:
    try:
        if not path.is_file() or path.stat().st_size > MAX_PROMPT_CHARACTERS * 4:
            raise OSError
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        print(f"Error: cannot read AI instruction file: {path}", file=sys.stderr)
        return None


COMMAND = Command(
    name="ai",
    help="Configure local message processing with Ollama.",
    handler=run,
    configure=configure,
)
