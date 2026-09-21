from __future__ import annotations

import json
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

from config import SETTINGS
from database import connect, tx
from rules_tools import ROUTES

POLICY_VERSION = "guardrails-v3"
DEFAULT_POLICY: dict[str, Any] = {
    "routes": dict(ROUTES),
    "courtesy_limit": SETTINGS.courtesy_limit,
    "high_agreement_threshold": 0.7,
    "human_review_agreement_threshold": 0.55,
    "high_confidence_threshold": 90,
}
PROTECTED_GUARDRAILS = [
    "Language models never receive mutation tools.",
    "Every mutation requires persisted deterministic policy authorization.",
    "Duplicate closure requires authoritative history and an older canonical claim.",
    "Courtesy resolution requires exact identity, claim, currency, and discrepancy verification.",
    "Critical, stale, late, conflicting, or over-budget work cannot become more autonomous.",
    "Idempotency is mandatory for all automated mutation actions.",
    "Human overrides require reviewer identity and a reason and never erase the original decision.",
]


def validate_policy(policy: dict[str, Any]) -> dict[str, Any]:
    required_categories = {"DB", "DM", "MC", "LP", "CR"}
    routes = policy.get("routes")
    if not isinstance(routes, dict) or set(routes) != required_categories:
        raise ValueError("Routes must contain exactly DB, DM, MC, LP, and CR.")
    if any(not str(value).strip() for value in routes.values()):
        raise ValueError("Every category must have a non-empty destination team.")
    try:
        limit = Decimal(str(policy.get("courtesy_limit")))
    except InvalidOperation as error:
        raise ValueError("Courtesy limit must be a valid decimal number.") from error
    if not limit.is_finite() or limit <= 0:
        raise ValueError("Courtesy limit must be finite and greater than zero.")
    high_agreement = float(policy.get("high_agreement_threshold"))
    review_agreement = float(policy.get("human_review_agreement_threshold"))
    confidence = int(policy.get("high_confidence_threshold"))
    if not 0 <= review_agreement < high_agreement <= 1:
        raise ValueError("Agreement thresholds must satisfy 0 <= review < high <= 1.")
    if not 0 <= confidence <= 100:
        raise ValueError("High-confidence threshold must be between 0 and 100.")
    return {
        "routes": {key: str(value).strip() for key, value in routes.items()},
        "courtesy_limit": format(limit, "f"),
        "high_agreement_threshold": high_agreement,
        "human_review_agreement_threshold": review_agreement,
        "high_confidence_threshold": confidence,
    }


def active_policy() -> dict[str, Any]:
    connection = connect()
    try:
        row = connection.execute(
            "SELECT settings FROM configuration_versions ORDER BY effective_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
    finally:
        connection.close()
    if not row:
        return dict(DEFAULT_POLICY)
    try:
        return validate_policy(json.loads(row["settings"]))
    except (json.JSONDecodeError, ValueError, TypeError):
        return dict(DEFAULT_POLICY)


def publish_policy(policy: dict[str, Any], author: str) -> str:
    if not author.strip():
        raise ValueError("Policy author is required.")
    validated = validate_policy(policy)
    config_id = f"CFG-{uuid.uuid4().hex}"
    with tx() as connection:
        connection.execute(
            "INSERT INTO configuration_versions(config_id,author,settings,protected_policy_version) VALUES(?,?,?,?)",
            (config_id, author.strip(), json.dumps(validated, sort_keys=True), POLICY_VERSION),
        )
        connection.execute(
            "INSERT INTO audit_events VALUES(?,?,?,?,?,?,CURRENT_TIMESTAMP)",
            (
                f"EVT-{uuid.uuid4().hex}",
                None,
                None,
                author.strip(),
                "POLICY_CONFIGURATION_PUBLISHED",
                json.dumps({"config_id": config_id, "settings": validated}),
            ),
        )
    return config_id


def active_policy_snapshot() -> tuple[str, dict[str, Any]]:
    """Return one immutable policy snapshot and its version identifier."""
    connection = connect()
    try:
        row = connection.execute(
            "SELECT config_id,settings FROM configuration_versions ORDER BY effective_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
    finally:
        connection.close()
    if not row:
        return "DEFAULT", dict(DEFAULT_POLICY)
    try:
        return str(row["config_id"]), validate_policy(json.loads(row["settings"]))
    except (json.JSONDecodeError, ValueError, TypeError):
        return "DEFAULT", dict(DEFAULT_POLICY)
