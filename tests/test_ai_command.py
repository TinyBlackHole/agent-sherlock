from types import SimpleNamespace

from agent_sherlock import ai as ai_settings
from agent_sherlock.cli import main
from agent_sherlock.commands import ai as ai_command
from agent_sherlock.integrations.ollama import OllamaModel


class FakeOllamaClient:
    def __init__(self, _base_url, **_kwargs):
        pass

    def list_models(self):
        return (
            OllamaModel(name="qwen2.5:7b", digest="digest-7b", size=4_000),
            OllamaModel(name="tiny:latest", digest="digest-tiny", size=1_000),
        )

    def chat(self, **_kwargs):
        return SimpleNamespace(
            supplement="Synthetic summary.",
            model="qwen2.5:7b",
        )


def test_ai_command_is_registered(capsys):
    assert main(["ai"]) == 0
    output = capsys.readouterr().out
    assert "sherlock ai" in output
    assert "models" in output
    assert "prompt" in output


def test_ai_connect_validates_and_saves_selected_model(monkeypatch, capsys):
    monkeypatch.setattr(ai_command, "OllamaClient", FakeOllamaClient)

    assert (
        main(
            [
                "ai",
                "connect",
                "ollama",
                "--model",
                "qwen2.5:7b",
            ]
        )
        == 0
    )

    config = ai_settings.load_ai_config()
    assert config.enabled
    assert config.model == "qwen2.5:7b"
    assert config.model_digest == "digest-7b"
    assert "Local AI enabled" in capsys.readouterr().out


def test_ai_model_and_prompt_are_easy_to_change(monkeypatch, capsys):
    monkeypatch.setattr(ai_command, "OllamaClient", FakeOllamaClient)
    ai_settings.save_ai_config(
        ai_settings.AIConfig(
            enabled=True,
            model="qwen2.5:7b",
            model_digest="digest-7b",
        )
    )

    assert main(["ai", "model", "set", "tiny:latest"]) == 0
    assert (
        main(
            [
                "ai",
                "prompt",
                "set",
                "Lee este correo y explícalo en 20 palabras.",
            ]
        )
        == 0
    )
    assert main(["ai", "mode", "replace"]) == 0

    config = ai_settings.load_ai_config()
    assert config.model == "tiny:latest"
    assert config.model_digest == "digest-tiny"
    assert config.prompt == "Lee este correo y explícalo en 20 palabras."
    assert config.mode == "replace"
    assert "New, unprocessed messages" in capsys.readouterr().out


def test_ai_models_marks_the_selected_model(monkeypatch, capsys):
    monkeypatch.setattr(ai_command, "OllamaClient", FakeOllamaClient)
    ai_settings.save_ai_config(
        ai_settings.AIConfig(model="qwen2.5:7b", model_digest="digest-7b")
    )

    assert main(["ai", "models"]) == 0

    output = capsys.readouterr().out
    assert "qwen2.5:7b (selected)" in output
    assert "tiny:latest" in output


def test_ai_disable_preserves_settings(monkeypatch, capsys):
    ai_settings.save_ai_config(
        ai_settings.AIConfig(
            enabled=True,
            model="qwen2.5:7b",
            prompt="Draft a reply.",
        )
    )

    assert main(["ai", "disable"]) == 0

    config = ai_settings.load_ai_config()
    assert not config.enabled
    assert config.model == "qwen2.5:7b"
    assert config.prompt == "Draft a reply."
    assert "forwarded literally" in capsys.readouterr().out


def test_ai_enable_status_mode_and_test(monkeypatch, capsys):
    monkeypatch.setattr(ai_settings, "OllamaClient", FakeOllamaClient)
    monkeypatch.setattr(ai_command, "OllamaClient", FakeOllamaClient)
    ai_settings.save_ai_config(
        ai_settings.AIConfig(
            enabled=False,
            model="qwen2.5:7b",
            prompt="Summarize.\nDraft a reply.",
        )
    )

    assert main(["ai", "enable"]) == 0
    assert main(["ai", "mode"]) == 0
    assert main(["ai", "status"]) == 0
    assert main(["ai", "test"]) == 0

    output = capsys.readouterr().out
    assert "Local AI enabled" in output
    assert "AI delivery mode: augment" in output
    assert "Ollama status: ready" in output
    assert "  Summarize." in output
    assert "  Draft a reply." in output
    assert "Synthetic summary." in output


def test_ai_prompt_file_normalizes_windows_line_endings(tmp_path):
    prompt_file = tmp_path / "instruction.txt"
    prompt_file.write_bytes(b"Summarize this.\r\nDraft a reply.\r")

    assert main(["ai", "prompt", "set", "--file", str(prompt_file)]) == 0

    assert ai_settings.load_ai_config().prompt == "Summarize this.\nDraft a reply."


def test_ai_prompt_configuration_failure_returns_operational_error(
    monkeypatch,
    capsys,
):
    def fail_save(_config):
        raise ai_settings.AIConfigurationError("configuration is read-only")

    monkeypatch.setattr(ai_command, "save_ai_config", fail_save)

    assert main(["ai", "prompt", "set", "Summarize."]) == 1
    assert "configuration is read-only" in capsys.readouterr().err


def test_ai_connect_rejects_remote_endpoint(capsys):
    assert (
        main(
            [
                "ai",
                "connect",
                "ollama",
                "--model",
                "qwen2.5:7b",
                "--base-url",
                "http://example.com:11434",
            ]
        )
        == 1
    )
    assert "localhost" in capsys.readouterr().err
