"""Deterministic validation layer (S4): engine, rules, conflicts.

Owner: Validation Worker V (``docs/06-multi-agent-collaboration.md`` §5.2). This
package is the only place allowed to change the rule engine; MCP tools (M) and the
repair node (A) consume :func:`validate` read-only.

Usage
    >>> from gameconfig_copilot.mcp_server.validator import load_constraints, validate
    >>> report = validate(draft, load_constraints(), snapshot)

The rule modules are imported here for their registration side effects;
:func:`validate` also imports them lazily so importing ``engine`` alone is safe.
"""

from __future__ import annotations

from . import business_rules, conflicts, schema_rules  # noqa: F401  (registration side effects)
from .engine import (
    DEFAULT_CONSTRAINTS_DIR,
    RULE_REGISTRY,
    STRUCTURAL_RULES,
    ConstraintLoadError,
    FieldRef,
    RepoSnapshot,
    Rule,
    RuleContext,
    RuleLayer,
    StructuralRule,
    TaskRecord,
    UnregisteredRuleError,
    constraint_error,
    field_tokens,
    load_builtin_rules,
    load_constraints,
    parse_constraints,
    preview,
    register,
    register_structural,
    render_message,
    resolve_scope,
    structural_error,
    validate,
)

__all__ = [
    "DEFAULT_CONSTRAINTS_DIR",
    "RULE_REGISTRY",
    "STRUCTURAL_RULES",
    "ConstraintLoadError",
    "FieldRef",
    "RepoSnapshot",
    "Rule",
    "RuleContext",
    "RuleLayer",
    "StructuralRule",
    "TaskRecord",
    "UnregisteredRuleError",
    "constraint_error",
    "field_tokens",
    "load_builtin_rules",
    "load_constraints",
    "parse_constraints",
    "preview",
    "register",
    "register_structural",
    "render_message",
    "resolve_scope",
    "structural_error",
    "validate",
]
