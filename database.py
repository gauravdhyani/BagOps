from __future__ import annotations

import csv
import hashlib
import html
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from config import SETTINGS


def connect():
    connection = sqlite3.connect(SETTINGS.db_path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


@contextmanager
def tx():
    connection = connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS claims(claim_id TEXT PRIMARY KEY,passenger_name TEXT NOT NULL,passenger_status TEXT,claim_status TEXT NOT NULL DEFAULT 'new',submitted_at TEXT,body_text TEXT NOT NULL,raw_input TEXT NOT NULL,input_hash TEXT NOT NULL,ingestion_status TEXT NOT NULL,validation_errors TEXT NOT NULL DEFAULT '[]',version INTEGER NOT NULL DEFAULT 1,assigned_team TEXT,automation_status TEXT NOT NULL DEFAULT 'PENDING',review_reason TEXT,review_priority TEXT NOT NULL DEFAULT 'STANDARD',immediate_attention INTEGER NOT NULL DEFAULT 0,attention_reason TEXT,review_due_at TEXT,ack_status TEXT NOT NULL DEFAULT 'NOT_SENT',processing_token TEXT,processing_started_at TEXT,lease_expires_at TEXT,attempt_count INTEGER NOT NULL DEFAULT 0,last_worker_id TEXT,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS graph_runs(graph_run_id TEXT PRIMARY KEY,claim_id TEXT NOT NULL REFERENCES claims(claim_id),claim_version INTEGER NOT NULL,status TEXT NOT NULL,trigger_type TEXT NOT NULL,step_count INTEGER NOT NULL DEFAULT 0,llm_calls INTEGER NOT NULL DEFAULT 0,tool_calls INTEGER NOT NULL DEFAULT 0,input_tokens INTEGER NOT NULL DEFAULT 0,output_tokens INTEGER NOT NULL DEFAULT 0,duration_ms INTEGER NOT NULL DEFAULT 0,provider_requests INTEGER NOT NULL DEFAULT 0,provider_retries INTEGER NOT NULL DEFAULT 0,schema_repairs INTEGER NOT NULL DEFAULT 0,cache_hits INTEGER NOT NULL DEFAULT 0,fallbacks TEXT NOT NULL DEFAULT '[]',decision TEXT,policy_result TEXT,error_code TEXT,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,completed_at TEXT,policy_config_id TEXT,policy_snapshot TEXT);
CREATE TABLE IF NOT EXISTS node_runs(run_id TEXT PRIMARY KEY,graph_run_id TEXT NOT NULL REFERENCES graph_runs(graph_run_id),claim_id TEXT NOT NULL REFERENCES claims(claim_id),claim_version INTEGER NOT NULL,node TEXT NOT NULL,node_kind TEXT NOT NULL,status TEXT NOT NULL,invocation_reason TEXT,facts TEXT NOT NULL DEFAULT '{}',evidence TEXT NOT NULL DEFAULT '[]',error_code TEXT,scheduled_at TEXT,deadline_at TEXT,completed_at TEXT,duration_ms INTEGER NOT NULL DEFAULT 0,input_tokens INTEGER NOT NULL DEFAULT 0,output_tokens INTEGER NOT NULL DEFAULT 0,attempts INTEGER NOT NULL DEFAULT 1,cache_hit INTEGER NOT NULL DEFAULT 0,provider_requests INTEGER NOT NULL DEFAULT 0,provider_retries INTEGER NOT NULL DEFAULT 0,schema_repairs INTEGER NOT NULL DEFAULT 0,rejection_reason TEXT);
CREATE TABLE IF NOT EXISTS classifications(classification_id TEXT PRIMARY KEY,graph_run_id TEXT NOT NULL REFERENCES graph_runs(graph_run_id),claim_id TEXT NOT NULL REFERENCES claims(claim_id),phase TEXT NOT NULL,category TEXT,confidence REAL,urgency TEXT,agreement REAL,reason_codes TEXT,rationale TEXT,model_id TEXT,prompt_version TEXT,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS llm_cache(cache_key TEXT PRIMARY KEY,response_json TEXT NOT NULL,input_tokens INTEGER NOT NULL,output_tokens INTEGER NOT NULL,model_id TEXT NOT NULL,prompt_version TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS compensation_records(record_id TEXT PRIMARY KEY,passenger_name TEXT NOT NULL,claim_id TEXT,approved_amount TEXT,paid_amount TEXT,verified_discrepancy TEXT,currency TEXT,record_status TEXT,hold_flags TEXT NOT NULL DEFAULT '[]',prior_resolution_id TEXT,raw_input TEXT NOT NULL,validation_errors TEXT NOT NULL DEFAULT '[]');
CREATE TABLE IF NOT EXISTS policy_authorizations(authorization_id TEXT PRIMARY KEY,graph_run_id TEXT NOT NULL REFERENCES graph_runs(graph_run_id),claim_id TEXT NOT NULL REFERENCES claims(claim_id),action_type TEXT NOT NULL,authorized INTEGER NOT NULL,reason_codes TEXT NOT NULL,evidence_hash TEXT NOT NULL,approved_payload_hash TEXT,policy_config_id TEXT,authorization_mode TEXT NOT NULL DEFAULT 'LIVE',consumed_at TEXT,consumed_action_id TEXT,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS actions(action_id TEXT PRIMARY KEY,claim_id TEXT NOT NULL REFERENCES claims(claim_id),graph_run_id TEXT NOT NULL REFERENCES graph_runs(graph_run_id),actor_type TEXT NOT NULL,action_type TEXT NOT NULL,idempotency_key TEXT UNIQUE NOT NULL,payload_hash TEXT NOT NULL,policy_authorization_id TEXT NOT NULL REFERENCES policy_authorizations(authorization_id),status TEXT NOT NULL,request_payload TEXT NOT NULL,response_payload TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS communications(communication_id TEXT PRIMARY KEY,claim_id TEXT NOT NULL REFERENCES claims(claim_id),action_id TEXT NOT NULL REFERENCES actions(action_id),kind TEXT NOT NULL,channel TEXT NOT NULL,message TEXT NOT NULL,delivery_status TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS courtesy_resolutions(resolution_id TEXT PRIMARY KEY,record_id TEXT NOT NULL REFERENCES compensation_records(record_id),claim_id TEXT NOT NULL REFERENCES claims(claim_id),action_id TEXT NOT NULL REFERENCES actions(action_id),amount TEXT NOT NULL,currency TEXT NOT NULL,method TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL,issued_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS claim_relationships(relationship_id TEXT PRIMARY KEY,graph_run_id TEXT NOT NULL REFERENCES graph_runs(graph_run_id),claim_id TEXT NOT NULL REFERENCES claims(claim_id),related_claim_id TEXT NOT NULL REFERENCES claims(claim_id),relationship_type TEXT NOT NULL,evidence TEXT NOT NULL,similarity REAL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS human_overrides(override_id TEXT PRIMARY KEY,claim_id TEXT NOT NULL REFERENCES claims(claim_id),graph_run_id TEXT NOT NULL REFERENCES graph_runs(graph_run_id),reviewer TEXT NOT NULL,original_decision TEXT NOT NULL,override_decision TEXT NOT NULL,reason TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS audit_events(event_id TEXT PRIMARY KEY,claim_id TEXT,graph_run_id TEXT,actor TEXT NOT NULL,event_type TEXT NOT NULL,details TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS configuration_versions(config_id TEXT PRIMARY KEY,author TEXT NOT NULL,settings TEXT NOT NULL,protected_policy_version TEXT NOT NULL,effective_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS regression_cases(case_id TEXT PRIMARY KEY,name TEXT NOT NULL,claim_text TEXT NOT NULL,settings TEXT NOT NULL,expected TEXT,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
"""

MIGRATIONS = {
    "claims": {"passenger_status": "TEXT", "claim_status": "TEXT NOT NULL DEFAULT 'new'", "submitted_at": "TEXT", "assigned_team": "TEXT", "review_reason": "TEXT", "created_at": "TEXT", "updated_at": "TEXT", "processing_token": "TEXT", "processing_started_at": "TEXT", "lease_expires_at": "TEXT", "attempt_count": "INTEGER NOT NULL DEFAULT 0", "last_worker_id": "TEXT", "review_priority": "TEXT NOT NULL DEFAULT 'STANDARD'", "immediate_attention": "INTEGER NOT NULL DEFAULT 0", "attention_reason": "TEXT", "review_due_at": "TEXT", "ack_status": "TEXT NOT NULL DEFAULT 'NOT_SENT'"},
    "graph_runs": {"step_count": "INTEGER NOT NULL DEFAULT 0", "llm_calls": "INTEGER NOT NULL DEFAULT 0", "tool_calls": "INTEGER NOT NULL DEFAULT 0", "input_tokens": "INTEGER NOT NULL DEFAULT 0", "output_tokens": "INTEGER NOT NULL DEFAULT 0", "duration_ms": "INTEGER NOT NULL DEFAULT 0", "provider_requests": "INTEGER NOT NULL DEFAULT 0", "provider_retries": "INTEGER NOT NULL DEFAULT 0", "schema_repairs": "INTEGER NOT NULL DEFAULT 0", "cache_hits": "INTEGER NOT NULL DEFAULT 0", "fallbacks": "TEXT NOT NULL DEFAULT '[]'", "decision": "TEXT", "policy_result": "TEXT", "error_code": "TEXT", "completed_at": "TEXT", "policy_config_id": "TEXT", "policy_snapshot": "TEXT"},
    "node_runs": {"node_kind": "TEXT NOT NULL DEFAULT 'UNKNOWN'", "invocation_reason": "TEXT", "facts": "TEXT NOT NULL DEFAULT '{}'", "evidence": "TEXT NOT NULL DEFAULT '[]'", "error_code": "TEXT", "scheduled_at": "TEXT", "deadline_at": "TEXT", "completed_at": "TEXT", "duration_ms": "INTEGER NOT NULL DEFAULT 0", "input_tokens": "INTEGER NOT NULL DEFAULT 0", "output_tokens": "INTEGER NOT NULL DEFAULT 0", "attempts": "INTEGER NOT NULL DEFAULT 1", "cache_hit": "INTEGER NOT NULL DEFAULT 0", "provider_requests": "INTEGER NOT NULL DEFAULT 0", "provider_retries": "INTEGER NOT NULL DEFAULT 0", "schema_repairs": "INTEGER NOT NULL DEFAULT 0", "rejection_reason": "TEXT"},
    "actions": {"actor_type": "TEXT NOT NULL DEFAULT 'SYSTEM'", "payload_hash": "TEXT NOT NULL DEFAULT ''", "policy_authorization_id": "TEXT", "status": "TEXT NOT NULL DEFAULT 'UNKNOWN'", "request_payload": "TEXT NOT NULL DEFAULT '{}'", "response_payload": "TEXT NOT NULL DEFAULT '{}'", "created_at": "TEXT"},
    "classifications": {"phase": "TEXT NOT NULL DEFAULT 'LEGACY'", "confidence": "REAL", "urgency": "TEXT", "agreement": "REAL", "reason_codes": "TEXT NOT NULL DEFAULT '[]'", "rationale": "TEXT", "model_id": "TEXT", "prompt_version": "TEXT", "created_at": "TEXT"},
    "policy_authorizations": {"reason_codes": "TEXT NOT NULL DEFAULT '[]'", "evidence_hash": "TEXT NOT NULL DEFAULT ''", "approved_payload_hash": "TEXT", "policy_config_id": "TEXT", "authorization_mode": "TEXT NOT NULL DEFAULT 'LIVE'", "consumed_at": "TEXT", "consumed_action_id": "TEXT", "created_at": "TEXT"},
    "compensation_records": {"approved_amount": "TEXT", "paid_amount": "TEXT", "verified_discrepancy": "TEXT", "currency": "TEXT", "record_status": "TEXT", "hold_flags": "TEXT NOT NULL DEFAULT '[]'", "prior_resolution_id": "TEXT", "raw_input": "TEXT NOT NULL DEFAULT '{}'", "validation_errors": "TEXT NOT NULL DEFAULT '[]'"},
    "communications": {"kind": "TEXT", "channel": "TEXT NOT NULL DEFAULT 'SIMULATED'", "message": "TEXT NOT NULL DEFAULT ''", "delivery_status": "TEXT NOT NULL DEFAULT 'UNKNOWN'", "created_at": "TEXT"},
    "courtesy_resolutions": {"amount": "TEXT", "currency": "TEXT", "method": "TEXT", "status": "TEXT", "reason": "TEXT", "issued_at": "TEXT"},
    "human_overrides": {"reviewer": "TEXT", "original_decision": "TEXT NOT NULL DEFAULT '{}'", "override_decision": "TEXT NOT NULL DEFAULT '{}'", "reason": "TEXT", "created_at": "TEXT"},
}


def _apply_additive_migrations(connection):
    for table, columns in MIGRATIONS.items():
        existing = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns.items():
            if name not in existing:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def init_db():
    with tx() as connection:
        connection.executescript(SCHEMA)
        _apply_additive_migrations(connection)
        connection.execute("UPDATE claims SET created_at=COALESCE(created_at,CURRENT_TIMESTAMP)")
        connection.execute("UPDATE claims SET updated_at=COALESCE(updated_at,created_at,CURRENT_TIMESTAMP)")
        connection.execute("CREATE INDEX IF NOT EXISTS ix_queue ON claims(automation_status,claim_status,lease_expires_at,immediate_attention,review_priority,submitted_at)")
        connection.execute("CREATE INDEX IF NOT EXISTS ix_nodes ON node_runs(graph_run_id,completed_at)")


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def clean(value: Any) -> str:
    return re.sub(r"<[^>]+>", "", html.unescape(str(value or ""))).strip()[:12000]


def money(value):
    if not str(value or "").strip():
        return None, True
    try:
        number = Decimal(str(value).replace(",", "."))
        return (format(number, "f"), True) if number.is_finite() else (None, False)
    except InvalidOperation:
        return None, False


def ingest_claims(path):
    init_db(); count = 0
    with open(path, encoding="utf-8-sig", newline="") as file, tx() as connection:
        for row in csv.DictReader(file):
            raw = dict(row); errors = []; claim_id = str(row.get("claim_id", "")).strip()
            if not re.fullmatch(r"BC-\d{4,}", claim_id):
                errors.append("INVALID_CLAIM_ID"); claim_id = claim_id or f"REJECTED-{digest(raw)[:12]}"
            name = str(row.get("passenger_name", "")).strip()
            if not name: errors.append("MISSING_PASSENGER_NAME")
            body = clean(row.get("body_text"))
            if not body: errors.append("MISSING_BODY_TEXT")
            submitted = str(row.get("submitted_at", "")).strip()
            if submitted:
                try: datetime.fromisoformat(submitted.replace("Z", "+00:00"))
                except ValueError: errors.append("INVALID_SUBMITTED_AT"); submitted = ""
            input_hash = digest(raw)
            old = connection.execute("SELECT input_hash,version FROM claims WHERE claim_id=?", (claim_id,)).fetchone()
            version = 1 if not old else old["version"] + int(old["input_hash"] != input_hash)
            valid = not errors
            connection.execute("""INSERT INTO claims(claim_id,passenger_name,passenger_status,claim_status,submitted_at,body_text,raw_input,input_hash,ingestion_status,validation_errors,version,automation_status,review_reason,review_priority) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(claim_id) DO UPDATE SET passenger_name=excluded.passenger_name,passenger_status=excluded.passenger_status,claim_status=excluded.claim_status,submitted_at=excluded.submitted_at,body_text=excluded.body_text,raw_input=excluded.raw_input,input_hash=excluded.input_hash,ingestion_status=excluded.ingestion_status,validation_errors=excluded.validation_errors,version=excluded.version,automation_status=CASE WHEN claims.input_hash<>excluded.input_hash THEN excluded.automation_status ELSE claims.automation_status END,updated_at=CURRENT_TIMESTAMP""", (claim_id, name or "UNKNOWN", str(row.get("passenger_status") or row.get("passenger_tier") or "Standard"), str(row.get("claim_status") or row.get("status") or "new").lower(), submitted or None, body, json.dumps(raw), input_hash, "VALID" if valid else "NEEDS_REVIEW", json.dumps(errors), version, "PENDING" if valid else "HUMAN_REVIEW", ",".join(errors) or None, "STANDARD" if valid else "HIGH")); count += 1
    return count


def ingest_compensation(path):
    init_db(); count = 0
    with open(path, encoding="utf-8-sig", newline="") as file, tx() as connection:
        for row in csv.DictReader(file):
            raw = dict(row); errors = []; values = {}
            for field in ("approved_amount", "paid_amount", "verified_discrepancy"):
                values[field], ok = money(row.get(field)); errors += [] if ok else [f"INVALID_{field.upper()}"]
            currency = str(row.get("currency", "")).strip().upper()
            if currency and not re.fullmatch(r"[A-Z]{3}", currency): errors.append("INVALID_CURRENCY"); currency = ""
            record_id = str(row.get("record_id") or row.get("source_record_id") or f"COMP-{digest(raw)[:12]}")
            holds = [x.strip().upper() for x in str(row.get("hold_flags", "")).strip("[]").split(",") if x.strip()]
            connection.execute("INSERT OR REPLACE INTO compensation_records(record_id,passenger_name,claim_id,approved_amount,paid_amount,verified_discrepancy,currency,record_status,hold_flags,prior_resolution_id,raw_input,validation_errors) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (record_id, str(row.get("passenger_name", "")).strip() or "UNKNOWN", str(row.get("claim_id", "")).strip() or None, values["approved_amount"], values["paid_amount"], values["verified_discrepancy"], currency or None, str(row.get("record_status", "")).lower() or None, json.dumps(holds), str(row.get("prior_resolution_id", "")).strip() or None, json.dumps(raw), json.dumps(errors))); count += 1
    return count


def acquire(ids=None, limit=100, worker="local"):
    init_db()
    current = datetime.now(timezone.utc)
    lease = (current + timedelta(seconds=SETTINGS.lease_seconds)).isoformat()
    token = str(uuid.uuid4())
    with tx() as connection:
        params = [current.isoformat()]
        where = "(automation_status='PENDING' OR (automation_status='PROCESSING' AND lease_expires_at<?)) AND LOWER(COALESCE(claim_status,''))!='closed'"
        if ids:
            where += f" AND claim_id IN ({','.join('?' for _ in ids)})"
            params.extend(ids)
        rows = connection.execute(f"SELECT * FROM claims WHERE {where} ORDER BY submitted_at,claim_id LIMIT ?", (*params, limit)).fetchall()
        acquired = []
        for row in rows:
            changed = connection.execute("""UPDATE claims SET automation_status='PROCESSING',processing_token=?,processing_started_at=?,lease_expires_at=?,attempt_count=attempt_count+1,last_worker_id=?,updated_at=CURRENT_TIMESTAMP WHERE claim_id=? AND LOWER(COALESCE(claim_status,''))!='closed' AND (automation_status='PENDING' OR (automation_status='PROCESSING' AND lease_expires_at<?))""", (token, current.isoformat(), lease, worker, row["claim_id"], current.isoformat())).rowcount
            if changed:
                item = dict(row); item["processing_token"] = token; acquired.append(item)
        return acquired


class LeaseOwnershipLost(RuntimeError):
    pass


def set_status(cid, status, reason=None, immediate=False, priority="STANDARD", processing_token=None):
    due = (datetime.now(timezone.utc) + timedelta(minutes=15 if immediate else 240)).isoformat() if status == "HUMAN_REVIEW" else None
    with tx() as connection:
        args = (status, reason, int(immediate), priority, reason, due)
        if processing_token:
            result = connection.execute("UPDATE claims SET automation_status=?,review_reason=?,immediate_attention=?,review_priority=?,attention_reason=?,review_due_at=?,processing_token=NULL,lease_expires_at=NULL,updated_at=CURRENT_TIMESTAMP WHERE claim_id=? AND processing_token=? AND lease_expires_at>=?", (*args, cid, processing_token, datetime.now(timezone.utc).isoformat()))
            if result.rowcount != 1: raise LeaseOwnershipLost("PROCESSING_LEASE_OWNERSHIP_LOST")
        else:
            connection.execute("UPDATE claims SET automation_status=?,review_reason=?,immediate_attention=?,review_priority=?,attention_reason=?,review_due_at=?,processing_token=NULL,lease_expires_at=NULL,updated_at=CURRENT_TIMESTAMP WHERE claim_id=?", (*args, cid))
    return True
