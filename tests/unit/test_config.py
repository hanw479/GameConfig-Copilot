"""Configuration tests (S0-02): GCC_ prefix, .env, required key, lazy import."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError

from gameconfig_copilot.config import (
    SETTINGS_BACKEND,
    Settings,
    SettingsError,
    get_settings,
)

SRC_ROOT = Path(__file__).resolve().parents[2] / "src"

_FALLBACK_SCRIPT = textwrap.dedent(
    """
    import json
    import os
    import sys


    class _BlockPydanticSettings:
        \"\"\"Simulate a machine where pydantic-settings cannot be installed.\"\"\"

        def find_spec(self, fullname, path=None, target=None):
            if fullname == "pydantic_settings" or fullname.startswith("pydantic_settings."):
                raise ModuleNotFoundError("No module named 'pydantic_settings'")
            return None


    sys.meta_path.insert(0, _BlockPydanticSettings())

    from gameconfig_copilot.config import SETTINGS_BACKEND, Settings, SettingsError, get_settings

    env_file = sys.argv[1]
    result = {"backend": SETTINGS_BACKEND}

    os.environ["GCC_LLM_API_KEY"] = "from-process-env"
    result["env_wins_over_dotenv"] = Settings(_env_file=env_file).llm_api_key.get_secret_value()
    result["kwargs_win"] = Settings(_env_file=env_file, llm_model="kw-model").llm_model

    os.environ.pop("GCC_LLM_API_KEY")
    result["dotenv_used"] = Settings(_env_file=env_file).llm_api_key.get_secret_value()

    os.environ["GCC_LLM_API_KEY"] = "from-process-env-2"
    result["no_file_means_no_dotenv"] = Settings(_env_file=None).llm_model

    os.environ.pop("GCC_LLM_API_KEY")
    try:
        get_settings()
    except SettingsError as exc:
        result["missing_key_names_variable"] = "GCC_LLM_API_KEY" in str(exc)
    print(json.dumps(result))
    """
)


def test_defaults_and_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GCC_LLM_API_KEY", "sk-unit-test-key")
    settings = Settings(_env_file=None)

    assert settings.llm_provider == "deepseek"
    assert settings.llm_model == "deepseek-chat"
    assert settings.llm_base_url is None
    assert settings.llm_timeout_s == 30.0
    assert settings.llm_api_key.get_secret_value() == "sk-unit-test-key"


def test_all_llm_variables_are_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GCC_LLM_PROVIDER", "OpenAI_Compatible")
    monkeypatch.setenv("GCC_LLM_MODEL", "internal-7b")
    monkeypatch.setenv("GCC_LLM_BASE_URL", "https://gateway.local/v1/")
    monkeypatch.setenv("GCC_LLM_API_KEY", "sk-unit-test-key")
    monkeypatch.setenv("GCC_LLM_TIMEOUT_S", "12.5")
    settings = Settings(_env_file=None)

    assert settings.llm_provider == "openai_compatible"
    assert settings.llm_model == "internal-7b"
    assert settings.llm_base_url == "https://gateway.local/v1"
    assert settings.llm_timeout_s == 12.5


def test_missing_api_key_raises_validation_error() -> None:
    with pytest.raises(ValidationError, match="llm_api_key"):
        Settings(_env_file=None)


def test_get_settings_names_the_missing_variable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A developer checkout may have a real .env; this test asserts the
    # fresh-checkout behavior, so run it outside the repository directory.
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SettingsError) as excinfo:
        get_settings()

    message = str(excinfo.value)
    assert "GCC_LLM_API_KEY" in message
    assert isinstance(excinfo.value.__cause__, ValidationError)


def test_empty_api_key_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GCC_LLM_API_KEY", "   ")
    with pytest.raises(ValidationError, match="API key"):
        Settings(_env_file=None)


@pytest.mark.parametrize("timeout", ["0", "-1", "601", "not-a-number"])
def test_invalid_timeout_is_rejected(monkeypatch: pytest.MonkeyPatch, timeout: str) -> None:
    monkeypatch.setenv("GCC_LLM_API_KEY", "sk-unit-test-key")
    monkeypatch.setenv("GCC_LLM_TIMEOUT_S", timeout)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_dotenv_is_loaded(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "GCC_LLM_API_KEY=from-dotenv\nGCC_LLM_MODEL=dotenv-model\n",
        encoding="utf-8",
    )
    settings = Settings(_env_file=env_file)

    assert settings.llm_api_key.get_secret_value() == "from-dotenv"
    assert settings.llm_model == "dotenv-model"


def test_environment_overrides_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("GCC_LLM_API_KEY=from-dotenv\n", encoding="utf-8")
    monkeypatch.setenv("GCC_LLM_API_KEY", "from-process-env")

    assert Settings(_env_file=env_file).llm_api_key.get_secret_value() == "from-process-env"


def test_import_without_api_key_succeeds() -> None:
    """A fresh checkout must be importable before any .env exists."""

    env = {key: value for key, value in os.environ.items() if not key.startswith("GCC_")}
    env["PYTHONIOENCODING"] = "utf-8"
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(part for part in (str(SRC_ROOT), existing) if part)

    proc = subprocess.run(
        [sys.executable, "-c", "import gameconfig_copilot; import gameconfig_copilot.cli"],
        cwd=SRC_ROOT.parent,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr


def test_settings_backend_self_reports() -> None:
    assert SETTINGS_BACKEND in {"pydantic-settings", "builtin-fallback"}


def test_builtin_fallback_keeps_contract_without_pydantic_settings(tmp_path: Path) -> None:
    """The install-free route must behave identically when the wheel is absent."""

    env_file = tmp_path / ".env"
    env_file.write_text(
        "GCC_LLM_API_KEY=from-dotenv\nGCC_LLM_MODEL=dotenv-model\n",
        encoding="utf-8",
    )
    env = {key: value for key, value in os.environ.items() if not key.startswith("GCC_")}
    env["PYTHONIOENCODING"] = "utf-8"
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(part for part in (str(SRC_ROOT), existing) if part)
    # Run from a directory without a .env so get_settings() must report the
    # missing variable instead of picking up the fixture file.
    clean_cwd = tmp_path / "cwd"
    clean_cwd.mkdir()

    proc = subprocess.run(
        [sys.executable, "-B", "-c", _FALLBACK_SCRIPT, str(env_file)],
        cwd=clean_cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )

    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["backend"] == "builtin-fallback"
    assert payload["env_wins_over_dotenv"] == "from-process-env"
    assert payload["kwargs_win"] == "kw-model"
    assert payload["dotenv_used"] == "from-dotenv"
    assert payload["no_file_means_no_dotenv"] == "deepseek-chat"
    assert payload["missing_key_names_variable"] is True
