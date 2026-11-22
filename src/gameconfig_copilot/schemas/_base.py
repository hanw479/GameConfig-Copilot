"""Shared helpers for the frozen data contracts.

All contract models use ``extra="forbid"`` so contract tests catch "多余字段"
early and the JSON Schema injected into prompts is exact.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"


class ContractModel(BaseModel):
    """Base class for all cross-layer contracts."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=False,
    )


def export_model_schema(
    model: type[BaseModel],
    *,
    schema_id: str,
    mode: str = "validation",
) -> dict[str, Any]:
    """Return a standalone JSON Schema document for ``model``.

    ``by_alias=True`` keeps wire field names (for example ``checked.schema``)
    instead of Python-safe aliases.
    """

    schema = model.model_json_schema(mode=mode, by_alias=True)
    schema["$schema"] = JSON_SCHEMA_DIALECT
    schema["$id"] = schema_id
    return schema
