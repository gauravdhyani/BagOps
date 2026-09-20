from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from database import digest, tx

ALLOWED = {"ACKNOWLEDGE", "ROUTE", "CLOSE_DUPLICATE", "REQUEST_INFORMATION", "COURTESY_RESOLUTION"}


def decimal_value(value):
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except Exception:
        return None


def authorize(action, claim, facts, graph_run_id, policy_snapshot=None, approved_payload=None, simulation=False):
    action = action.upper()
    policy_snapshot = policy_snapshot or {}
    reasons = []
    unresolved = facts.get("conflicts", {}).get("unresolved", {})
    if facts.get("critical"):
        reasons.append("CRITICAL_REVIEW_REQUIRED")
    if unresolved:
        reasons.append("AUTHORITATIVE_CONFLICT")
    if facts.get("prompt_injection") and action in {"CLOSE_DUPLICATE", "COURTESY_RESOLUTION"}:
        reasons.append("PROMPT_INJECTION_IRREVERSIBLE_REVIEW")

    if action == "CLOSE_DUPLICATE":
        evidence = facts.get("duplicate_evidence") or {}
        checks = {
            "HISTORY_UNAVAILABLE": facts.get("history_available"),
            "CANONICAL_MISSING": facts.get("canonical_claim_id"),
            "IDENTITY_MISMATCH": evidence.get("identity_match"),
            "INCIDENT_MISMATCH": evidence.get("incident_match"),
            "NEW_INFORMATION": not evidence.get("material_new_information", True),
            "OLDER_REQUIRED": evidence.get("older"),
        }
        reasons.extend(code for code, passed in checks.items() if not passed)
    elif action == "COURTESY_RESOLUTION":
        record = facts.get("compensation_record") or {}
        claimed = decimal_value(facts.get("claimed_difference"))
        verified = decimal_value(record.get("verified_discrepancy"))
        limit = decimal_value(policy_snapshot.get("courtesy_limit"))
        if record.get("status") != "SUCCESS": reasons.append("COMPENSATION_RECORD_MISSING")
        if not record.get("exact_claim_match"): reasons.append("CLAIM_REFERENCE_MISMATCH")
        if facts.get("claim_reference") and facts["claim_reference"] != claim["claim_id"]: reasons.append("NARRATIVE_REFERENCE_MISMATCH")
        if not record.get("exact_identity_match"): reasons.append("IDENTITY_MISMATCH")
        if facts.get("currency") != record.get("currency"): reasons.append("CURRENCY_MISMATCH")
        if claimed is None or verified is None or claimed.quantize(Decimal(".01")) != verified.quantize(Decimal(".01")): reasons.append("DISCREPANCY_NOT_VERIFIED")
        if verified is None or verified <= 0: reasons.append("DISCREPANCY_NOT_POSITIVE")
        elif limit is None or verified > limit: reasons.append("EXCEEDS_AUTHORITY")
        if record.get("record_status") not in {"verified", "paid"}: reasons.append("PAYMENT_STATUS_BLOCKED")
        if record.get("hold_flags"): reasons.append("HOLD_PRESENT")
        if record.get("prior_resolution_id"): reasons.append("PRIOR_RESOLUTION_EXISTS")
    elif action == "ROUTE" and (not facts.get("final_category") or not facts.get("destination_team")):
        reasons.append("ROUTE_FACTS_MISSING")
    elif action == "REQUEST_INFORMATION" and not facts.get("missing_information"):
        reasons.append("REQUEST_NOT_SPECIFIC")
    elif action not in ALLOWED | {"HUMAN_REVIEW"}:
        reasons.append("ACTION_NOT_ALLOWED")

    would_authorize = not reasons and action != "HUMAN_REVIEW"
    approved = would_authorize and not simulation
    authorization_id = ("AUTH-" if approved else "SIM-" if simulation else "DENY-") + uuid.uuid4().hex
    payload_hash = digest(approved_payload) if would_authorize and approved_payload is not None else None
    result = {
        "authorization_id": authorization_id if approved else None,
        "action": action,
        "authorized": approved,
        "would_authorize": would_authorize,
        "simulation": simulation,
        "reason_codes": reasons or (["POLICY_WOULD_APPROVE"] if simulation else ["POLICY_APPROVED"]),
        "approved_payload_hash": payload_hash,
        "policy_config_id": facts.get("policy_config_id"),
    }
    with tx() as connection:
        connection.execute(
            """INSERT INTO policy_authorizations(
                authorization_id,graph_run_id,claim_id,action_type,authorized,
                reason_codes,evidence_hash,approved_payload_hash,policy_config_id,authorization_mode
            ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (authorization_id, graph_run_id, claim["claim_id"], action, int(approved),
             json.dumps(result["reason_codes"]), digest(facts), payload_hash,
             facts.get("policy_config_id"), "SIMULATION" if simulation else "LIVE"),
        )
    return result


def execute(action, claim_id, graph_run_id, authorization_id, idempotency_key, payload, processing_token):
    action = action.upper()
    if action not in ALLOWED:
        raise ValueError("ACTION_NOT_ALLOWED")
    payload_hash = digest(payload)
    with tx() as connection:
        # Read-only idempotent replay is allowed even after finalization.
        existing = connection.execute("SELECT * FROM actions WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        if existing:
            if existing["claim_id"] != claim_id or existing["action_type"] != action or existing["payload_hash"] != payload_hash:
                raise ValueError("IDEMPOTENCY_CONFLICT")
            connection.execute(
                "INSERT INTO audit_events VALUES(?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                (f"EVT-{uuid.uuid4().hex}", claim_id, graph_run_id, "system", "IDEMPOTENT_REPLAY", json.dumps({"action_id": existing["action_id"]})),
            )
            return {"action_id": existing["action_id"], "status": existing["status"], "idempotent_replay": True}

        current_time = datetime.now(timezone.utc).isoformat()
        lease = connection.execute(
            "SELECT automation_status,processing_token,lease_expires_at FROM claims WHERE claim_id=?", (claim_id,)
        ).fetchone()
        if (not lease or lease["automation_status"] != "PROCESSING" or
                lease["processing_token"] != processing_token or not lease["lease_expires_at"] or
                lease["lease_expires_at"] < current_time):
            raise PermissionError("PROCESSING_LEASE_OWNERSHIP_LOST")

        authorization = connection.execute(
            """SELECT approved_payload_hash FROM policy_authorizations
            WHERE authorization_id=? AND graph_run_id=? AND claim_id=? AND action_type=?
              AND authorized=1 AND authorization_mode='LIVE' AND consumed_at IS NULL""",
            (authorization_id, graph_run_id, claim_id, action),
        ).fetchone()
        if not authorization:
            raise PermissionError("AUTHORIZATION_REQUIRED")
        if not authorization["approved_payload_hash"]:
            raise PermissionError("APPROVED_PAYLOAD_HASH_REQUIRED")
        if authorization["approved_payload_hash"] != payload_hash:
            raise PermissionError("AUTHORIZED_PAYLOAD_MISMATCH")

        action_id = f"ACT-{uuid.uuid4().hex}"
        consumed = connection.execute(
            """UPDATE policy_authorizations SET consumed_at=CURRENT_TIMESTAMP,consumed_action_id=?
            WHERE authorization_id=? AND graph_run_id=? AND claim_id=? AND action_type=?
              AND authorized=1 AND authorization_mode='LIVE' AND consumed_at IS NULL""",
            (action_id, authorization_id, graph_run_id, claim_id, action),
        )
        if consumed.rowcount != 1:
            raise PermissionError("AUTHORIZATION_INVALID_OR_ALREADY_CONSUMED")

        response = {"result": "success"}
        communication = None
        resolution = None
        if action == "ROUTE":
            connection.execute("UPDATE claims SET claim_status='open',assigned_team=? WHERE claim_id=?", (payload.get("destination_team"), claim_id))
        elif action == "CLOSE_DUPLICATE":
            connection.execute("UPDATE claims SET claim_status='closed' WHERE claim_id=?", (claim_id,))
        elif action in {"ACKNOWLEDGE", "REQUEST_INFORMATION"}:
            if action == "ACKNOWLEDGE":
                message = payload.get("message") or "We received your baggage claim and will keep you updated."
                kind = "ACKNOWLEDGMENT"
            else:
                message = payload.get("message") or "Please provide: " + ", ".join(payload.get("missing_information", []))
                kind = "INFORMATION_REQUEST"
            if not message.strip():
                raise ValueError("COMMUNICATION_MESSAGE_REQUIRED")
            communication = (f"COM-{uuid.uuid4().hex}", kind, message)
        else:
            record_id = payload.get("record_id")
            authoritative = connection.execute(
                "SELECT * FROM compensation_records WHERE record_id=? AND claim_id=?", (record_id, claim_id)
            ).fetchone()
            if not authoritative:
                raise ValueError("AUTHORIZED_COMPENSATION_RECORD_NOT_FOUND")
            holds = json.loads(authoritative["hold_flags"] or "[]")
            verified = decimal_value(authoritative["verified_discrepancy"])
            if authoritative["record_status"] not in {"verified", "paid"}: raise PermissionError("PAYMENT_STATUS_BLOCKED")
            if holds: raise PermissionError("HOLD_PRESENT")
            if authoritative["prior_resolution_id"]: raise PermissionError("PRIOR_RESOLUTION_EXISTS")
            if verified is None or verified <= 0: raise PermissionError("DISCREPANCY_NOT_POSITIVE")
            if str(authoritative["verified_discrepancy"]) != str(payload.get("verified_discrepancy")) or authoritative["currency"] != payload.get("currency"):
                raise PermissionError("AUTHORIZED_COMPENSATION_FACTS_CHANGED")
            resolution = (f"RES-{uuid.uuid4().hex}", record_id, str(authoritative["verified_discrepancy"]), authoritative["currency"])
            if connection.execute(
                "UPDATE compensation_records SET prior_resolution_id=? WHERE record_id=? AND prior_resolution_id IS NULL",
                (resolution[0], record_id),
            ).rowcount != 1:
                raise PermissionError("ALREADY_RESOLVED")

        connection.execute(
            """INSERT INTO actions(action_id,claim_id,graph_run_id,actor_type,action_type,idempotency_key,
                payload_hash,policy_authorization_id,status,request_payload,response_payload)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (action_id, claim_id, graph_run_id, "SYSTEM", action, idempotency_key, payload_hash,
             authorization_id, "SUCCESS", json.dumps(payload, default=str), json.dumps(response)),
        )
        if communication:
            connection.execute(
                "INSERT INTO communications(communication_id,claim_id,action_id,kind,channel,message,delivery_status) VALUES(?,?,?,?,?,?,?)",
                (communication[0], claim_id, action_id, communication[1], "SIMULATED", communication[2], "QUEUED"),
            )
            if action == "ACKNOWLEDGE":
                connection.execute("UPDATE claims SET ack_status='QUEUED',updated_at=CURRENT_TIMESTAMP WHERE claim_id=?", (claim_id,))
        if resolution:
            connection.execute(
                "INSERT INTO courtesy_resolutions(resolution_id,record_id,claim_id,action_id,amount,currency,method,status,reason) VALUES(?,?,?,?,?,?,?,?,?)",
                (resolution[0], resolution[1], claim_id, action_id, resolution[2], resolution[3], "SIMULATED_CREDIT", "ISSUED", "Verified shortfall"),
            )
        connection.execute(
            "INSERT INTO audit_events VALUES(?,?,?,?,?,?,CURRENT_TIMESTAMP)",
            (f"EVT-{uuid.uuid4().hex}", claim_id, graph_run_id, "system", f"ACTION_{action}", json.dumps({"action_id": action_id})),
        )
        return {"action_id": action_id, "status": "SUCCESS", "idempotent_replay": False}


def save_override(claim_id, graph_run_id, reviewer, decision, human_action, reason):
    if not reviewer.strip() or not reason.strip():
        raise ValueError("Reviewer and reason required")
    allowed = {"HUMAN_CLOSE", "HUMAN_REOPEN", "HUMAN_ROUTE", "HUMAN_ACKNOWLEDGE", "HUMAN_REQUEST_INFORMATION", "HUMAN_NO_ACTION"}
    if human_action not in allowed:
        raise ValueError("UNSUPPORTED_HUMAN_ACTION")
    override_id=f"OVR-{uuid.uuid4().hex}"; authorization_id=f"HUMAN-{uuid.uuid4().hex}"; action_id=f"ACT-{uuid.uuid4().hex}"; payload={**decision,"human_action":human_action}
    with tx() as connection:
        run=connection.execute("SELECT decision FROM graph_runs WHERE graph_run_id=? AND claim_id=?",(graph_run_id,claim_id)).fetchone()
        if not run: raise ValueError("Graph run not found")
        connection.execute("INSERT INTO human_overrides(override_id,claim_id,graph_run_id,reviewer,original_decision,override_decision,reason) VALUES(?,?,?,?,?,?,?)",(override_id,claim_id,graph_run_id,reviewer,run["decision"] or "{}",json.dumps(payload),reason))
        connection.execute("INSERT INTO policy_authorizations(authorization_id,graph_run_id,claim_id,action_type,authorized,reason_codes,evidence_hash,approved_payload_hash,authorization_mode,consumed_at,consumed_action_id) VALUES(?,?,?,?,1,?,?,?,'LIVE',CURRENT_TIMESTAMP,?)",(authorization_id,graph_run_id,claim_id,human_action,json.dumps(["HUMAN_OVERRIDE_AUTHORIZED"]),digest(payload),digest(payload),action_id))
        connection.execute("INSERT INTO actions(action_id,claim_id,graph_run_id,actor_type,action_type,idempotency_key,payload_hash,policy_authorization_id,status,request_payload,response_payload) VALUES(?,?,?,?,?,?,?,?,?,?,?)",(action_id,claim_id,graph_run_id,"HUMAN",human_action,f"{claim_id}:{graph_run_id}:{override_id}",digest(payload),authorization_id,"SUCCESS",json.dumps(payload),json.dumps({"result":"human_override"})))
        status={"HUMAN_CLOSE":"closed","HUMAN_REOPEN":"open","HUMAN_ROUTE":"open"}.get(human_action)
        connection.execute("UPDATE claims SET automation_status='HUMAN_DECIDED',claim_status=COALESCE(?,claim_status),assigned_team=COALESCE(?,assigned_team),updated_at=CURRENT_TIMESTAMP WHERE claim_id=?",(status,decision.get("destination_team"),claim_id))
        connection.execute("INSERT INTO audit_events VALUES(?,?,?,?,?,?,CURRENT_TIMESTAMP)",(f"EVT-{uuid.uuid4().hex}",claim_id,graph_run_id,reviewer,"HUMAN_OVERRIDE",json.dumps({"override_id":override_id,"action_id":action_id,"reason":reason})))
    return override_id
