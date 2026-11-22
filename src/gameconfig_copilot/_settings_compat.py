"""Built-in fallback for the small slice of ``pydantic-settings`` we rely on.

Why this exists
---------------
``pydantic-settings`` is the canonical backend and stays a declared runtime
dependency (``pyproject.toml``). Some environments cannot install wheels at all
(air-gapped checkouts, restricted CI images, the sandboxed review box used for
the S0 gate). Making ``config.py`` unimportable there would take the whole
package — CLI, schemas, tracer, LLM layer — down with it.

So this module reproduces *exactly* the behaviour ``config.py`` needs, and
nothing else:

* ``GCC_``-prefixed environment variables, case-insensitive by default;
* optional ``.env`` loading, with the process environment taking precedence;
* ``Settings(_env_file=...)`` / ``Settings(_env_file=None)`` constructor
  overrides, including the "no file" sentinel;
* explicit keyword arguments take precedence over both sources.

Field validation, ``SecretStr`` masking and ``ValidationError`` shapes are plain
Pydantic, so they are identical on both backends.

Out of scope on purpose (add `pydantic-settings` if you need them): nested
delimiters (``__``), ``secrets/`` integrations, dotenv variable interpolation,
and ``env_nested_max_split`` style options.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, TypedDict

from pydantic import BaseModel, ConfigDict

__all__ = ["BaseSettings", "ENV_FILE_SENTINEL", "SettingsConfigDict"]


class SettingsConfigDict(TypedDict, total=False):
    """Subset of pydantic-settings' ``SettingsConfigDict`` used by this project."""

    env_prefix: str
    env_file: str | os.PathLike[str] | None
    env_file_encoding: str
    case_sensitive: bool
    extra: str


class _EnvFileSentinel:
    """Marks "caller did not pass ``_env_file``" (mirrors pydantic-settings)."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "ENV_FILE_SENTINEL"


ENV_FILE_SENTINEL: Any = _EnvFileSentinel()


def _read_env_file(path: Path, *, encoding: str) -> dict[str, str]:
    """Parse a minimal ``KEY=VALUE`` dotenv file; missing file yields ``{}``."""

    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding=encoding)
    except OSError:  # pragma: no cover - unreadable file is treated as absent
        return {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if key:
            values[key] = value
    return values


def _environ(case_sensitive: bool) -> dict[str, str]:
    if case_sensitive:
        return dict(os.environ)
    return {key.upper(): value for key, value in os.environ.items()}


class BaseSettings(BaseModel):
    """Environment/dotenv-aware ``BaseModel`` (stand-in for pydantic-settings)."""

    model_config = ConfigDict(extra="ignore")

    def __init__(self, _env_file: Any = ENV_FILE_SENTINEL, **data: Any) -> None:
        settings_config: dict[str, Any] = dict(type(self).model_config or {})
        prefix = str(settings_config.get("env_prefix") or "")
        case_sensitive = bool(settings_config.get("case_sensitive", False))

        env_file = settings_config.get("env_file") if _env_file is ENV_FILE_SENTINEL else _env_file
        dotenv: dict[str, str] = {}
        if env_file is not None:
            encoding = str(settings_config.get("env_file_encoding") or "utf-8")
            dotenv = _read_env_file(Path(env_file).expanduser(), encoding=encoding)
            if not case_sensitive:
                dotenv = {name.upper(): value for name, value in dotenv.items()}

        environ = _environ(case_sensitive)
        resolved: dict[str, Any] = {}
        for field_name in type(self).model_fields:
            env_name = f"{prefix}{field_name}"
            lookup = env_name if case_sensitive else env_name.upper()
            if lookup in environ:
                resolved[field_name] = environ[lookup]
            elif lookup in dotenv:
                resolved[field_name] = dotenv[lookup]

        merged = {**resolved, **data}
        super().__init__(**merged)
