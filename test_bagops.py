import copy
import itertools
import json
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    import config
    import database

    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(config.SETTINGS, "db_path", db_path)
    monkeypatch.setattr(database.SETTINGS, "db_path", db_path)
    database.init_db()


def add(cid="BC-7001", body="My bag never arrived at the carousel.", status="VALID"):
    from database import digest, tx

    raw = {"claim_id": cid, "body_text": body}
    with tx() as connection:
        connection.execute(
            """INSERT INTO claims(
                claim_id,passenger_name,claim_status,body_text,raw_input,
                input_hash,ingestion_status,validation_errors,automation_status
            ) VALUES(?,'Ada','new',?,?,?,?,'[]','PENDING')""",
            (cid, body, json.dumps(raw), digest(raw), status),
        )


def result(
    node,
    facts,
    gid="G",
    version=1,
    run="R",
    status="SUCCESS",
    completed=None,
    deadline=None,
    kind="RULE",
):
    current = datetime.now(timezone.utc)
    completed = completed or current
    deadline = deadline or current + timedelta(seconds=10)
    return {
        "node": node,
        "run_id": run,
        "graph_run_id": gid,
        "claim_version": version,
        "status": status,
        "kind": kind,
        "facts": facts,
        "evidence": [],
        "error_code": None,
        "scheduled_at": current.isoformat(),
        "deadline_at": deadline.isoformat(),
        "completed_at": completed.isoformat(),
        "duration_ms": 1,
        "input_tokens": 0,
        "output_tokens": 0,
        "attempts": 1,
        "invocation_reason": "test",
        "cache_hit": False,
        "provider_requests": 0,
        "provider_retries": 0,
        "schema_repairs": 0,
    }


def fake_llm(role, text, context=None):
    metadata = {
        "cache_hit": False,
        "provider_requests": 1,
        "provider_retries": 0,
        "schema_repairs": 0,
    }
    if role == "triage":
        return (
            {
                "category": "DB",
                "confidence": 95,
                "urgency": "H",
                "secondary": [],
                "needs": ["HIST"],
                "ambiguous": False,
                "disposition": "CLAIM",
                "reason_codes": ["NOARR"],
                "rationale": "Checked bag did not arrive.",
            },
            10,
            5,
            1,
            metadata,
        )
    return (
        {
            "category": "DB",
            "confidence": 95,
            "urgency": "H",
            "action": "ROUTE",
            "destination_team": "Baggage Tracing",
            "missing_information": [],
            "expected_amount": None,
            "received_amount": None,
            "currency": None,
            "claim_reference": None,
            "reason_codes": ["VERIFIED"],
            "rationale": "Evidence supports routing.",
        },
        10,
        5,
        1,
        metadata,
    )


def prepare_action(action, payload, cid="BC-7001", auth="AUTH", gid="G"):
    from database import digest
    from database import tx

    add(cid)
    token = f"TOKEN-{cid}-{gid}"
    lease_expires = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    with tx() as connection:
        connection.execute(
            """UPDATE claims
            SET automation_status='PROCESSING',processing_token=?,lease_expires_at=?
            WHERE claim_id=?""",
            (token, lease_expires, cid),
        )
        connection.execute(
            """INSERT INTO graph_runs(
                graph_run_id,claim_id,claim_version,status,trigger_type
            ) VALUES(?,?,1,'RUNNING','TEST')""",
            (gid, cid),
        )
        connection.execute(
            """INSERT INTO policy_authorizations(
                authorization_id,graph_run_id,claim_id,action_type,authorized,
                reason_codes,evidence_hash,approved_payload_hash,authorization_mode
            ) VALUES(?,?,?,?,1,'[]','hash',?,'LIVE')""",
            (auth, gid, cid, action, digest(payload)),
        )
    return token


def test_result_acceptance_rejects_stale_late_duplicate():
    from reducers import accept_result

    state = {
        "graph_run_id": "G",
        "claim_version": 1,
        "status": "RUNNING",
        "accepted_run_ids": {"D"},
    }
    item = result("x", {}, gid="OLD")
    assert accept_result(state, item, item["completed_at"])[1] == "STALE_GRAPH_RUN"
    item = result("x", {}, version=2)
    assert accept_result(state, item, item["completed_at"])[1] == "STALE_CLAIM_VERSION"
    item = result("x", {}, run="D")
    assert accept_result(state, item, item["completed_at"])[1] == "DUPLICATE_RUN_ID"
    item = result(
        "x",
        {},
        completed=datetime.now(timezone.utc) + timedelta(seconds=20),
        deadline=datetime.now(timezone.utc),
    )
    assert accept_result(state, item, item["completed_at"])[1] == "NODE_DEADLINE_EXCEEDED"
    state["status"] = "COMPLETED"
    item = result("x", {})
    assert accept_result(state, item, item["completed_at"])[1] == "LATE_AFTER_TERMINAL"


def test_reducer_order_independent_and_conflict_preserved():
    from reducers import reduce_results

    rows = [
        result("rules", {"primary_category": "LP", "urgency": "M", "reason_codes": ["R"]}, run="1"),
        result("triage_agent", {"category": "DB", "urgency": "M", "reason_codes": ["T"]}, run="2"),
    ]
    outputs = [reduce_results(list(order)) for order in itertools.permutations(rows)]
    assert all(output == outputs[0] for output in outputs)
    assert "category" in outputs[0]["conflicts"]["unresolved"]


def test_monotonic_safety_late_critical():
    from reducers import reduce_results

    safe = result("triage_agent", {"category": "DB"}, run="1")
    critical = result("rules", {"primary_category": "DB", "is_critical": True}, run="2")
    assert reduce_results([safe, critical])["critical"] is True


def test_atomic_acquire_and_lease_recovery():
    from database import acquire, tx

    add()
    assert len(acquire(limit=1)) == 1
    assert acquire(limit=1) == []
    with tx() as connection:
        connection.execute(
            "UPDATE claims SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE claim_id='BC-7001'"
        )
    assert len(acquire(limit=1)) == 1


def test_dynamic_fast_path_one_llm(monkeypatch):
    import graph
    from database import acquire, connect

    add()
    monkeypatch.setattr(graph, "invoke", fake_llm)
    output = graph.run_agent(acquire(limit=1)[0])
    assert output["decision"]["recommended_action"] == "ROUTE"
    assert output["status"] == "COMPLETED"
    assert output["policy"]["authorized"] is True
    connection = connect()
    graph_run = connection.execute("SELECT llm_calls FROM graph_runs").fetchone()
    connection.close()
    assert graph_run[0] == 1


def test_completion_order_same_decision(monkeypatch):
    import graph
    from database import acquire

    monkeypatch.setattr(graph, "invoke", fake_llm)
    decisions = []
    orders = [
        ["rules", "triage_agent", "history_tool"],
        ["triage_agent", "history_tool", "rules"],
        ["history_tool", "rules", "triage_agent"],
    ]
    for index, order in enumerate(orders):
        cid = f"BC-70{index + 10}"
        add(cid)
        decisions.append(
            graph.run_agent(acquire([cid], 1)[0], dry_run=True, arrival_order=order)["decision"]
        )
    assert decisions[0] == decisions[1] == decisions[2]


def test_provider_circuit_breaker(monkeypatch):
    import graph
    from database import acquire

    add()
    monkeypatch.setattr(
        graph,
        "invoke",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("timeout")),
    )
    output = graph.run_agent(acquire(limit=1)[0])
    assert output["status"] == "HUMAN_REVIEW"
    assert not any(item["node"] == "reassessment_agent" for item in output["results"])


def test_history_failure_blocks_close_not_route(monkeypatch):
    import graph
    from database import acquire

    add()
    monkeypatch.setattr(graph, "invoke", fake_llm)
    output = graph.run_agent(acquire(limit=1)[0], failures={"history_tool": True})
    assert output["decision"]["recommended_action"] != "CLOSE_DUPLICATE"


def test_budget_exhaustion_persisted_review(monkeypatch):
    import config
    import graph
    from database import acquire, connect

    add()
    monkeypatch.setattr(graph, "invoke", fake_llm)
    monkeypatch.setattr(config.SETTINGS, "max_steps", 1)
    with pytest.raises(RuntimeError):
        graph.run_agent(acquire(limit=1)[0])
    connection = connect()
    assert connection.execute("SELECT status FROM graph_runs").fetchone()[0] == "FAILED"
    assert connection.execute("SELECT automation_status FROM claims").fetchone()[0] == "HUMAN_REVIEW"
    connection.close()


def test_prompt_injection_irreversible_block():
    from controls import authorize
    from database import tx

    add()
    with tx() as connection:
        connection.execute(
            "INSERT INTO graph_runs(graph_run_id,claim_id,claim_version,status,trigger_type) VALUES('G','BC-7001',1,'RUNNING','T')"
        )
    assert not authorize(
        "CLOSE_DUPLICATE", {"claim_id": "BC-7001"}, {"prompt_injection": True}, "G"
    )["authorized"]


def test_compensation_negative_and_reversed_blocked():
    from controls import authorize
    from database import tx

    add()
    with tx() as connection:
        connection.execute(
            "INSERT INTO graph_runs(graph_run_id,claim_id,claim_version,status,trigger_type) VALUES('G','BC-7001',1,'RUNNING','T')"
        )
    facts = {
        "claimed_difference": "-5",
        "currency": "USD",
        "compensation_record": {
            "status": "SUCCESS",
            "exact_claim_match": True,
            "exact_identity_match": True,
            "verified_discrepancy": "-5",
            "currency": "USD",
            "record_status": "reversed",
            "hold_flags": [],
        },
    }
    assert not authorize(
        "COURTESY_RESOLUTION", {"claim_id": "BC-7001"}, facts, "G"
    )["authorized"]


def test_invalid_ingestion_goes_review(tmp_path):
    from database import connect, ingest_claims

    csv_path = tmp_path / "claims.csv"
    csv_path.write_text("claim_id,passenger_name,body_text\nBAD,,\n", encoding="utf-8")
    ingest_claims(str(csv_path))
    connection = connect()
    assert connection.execute("SELECT automation_status FROM claims").fetchone()[0] == "HUMAN_REVIEW"
    connection.close()


def test_decimal_rejects_nonfinite():
    from rules_tools import decimal_value

    assert decimal_value("NaN") is None
    assert decimal_value("Infinity") is None
    assert decimal_value("USD 12.50") == pytest.approx(12.5)


def test_dry_run_counter(monkeypatch):
    import processor

    add()
    monkeypatch.setattr(
        processor,
        "run_agent",
        lambda *args, **kwargs: {"status": "DRY_RUN", "decision": {}},
    )
    assert processor.process(["BC-7001"], dry_run=True)["dry_run"] == 1


def test_policy_publish_and_active():
    from policy_store import active_policy, publish_policy

    policy = copy.deepcopy(active_policy())
    policy["courtesy_limit"] = "175.00"
    policy["routes"]["DB"] = "Priority Baggage Tracing"
    config_id = publish_policy(policy, "Policy Owner")
    assert config_id.startswith("CFG-")
    active = active_policy()
    assert active["courtesy_limit"] == "175.00"
    assert active["routes"]["DB"] == "Priority Baggage Tracing"


def test_policy_protected_validation():
    from policy_store import active_policy, publish_policy

    policy = copy.deepcopy(active_policy())
    policy["routes"].pop("CR")
    with pytest.raises(ValueError):
        publish_policy(policy, "Policy Owner")


@pytest.mark.parametrize(
    "action,payload",
    [
        ("ROUTE", {"destination_team": "Baggage Tracing"}),
        ("ACKNOWLEDGE", {}),
        ("REQUEST_INFORMATION", {"missing_information": ["bag tag"]}),
        ("CLOSE_DUPLICATE", {}),
    ],
)
def test_successful_actions_and_replay(action, payload):
    from controls import execute
    from database import connect

    token = prepare_action(action, payload)
    first = execute(action, "BC-7001", "G", "AUTH", f"KEY-{action}", payload, token)
    second = execute(action, "BC-7001", "G", "AUTH", f"KEY-{action}", payload, token)
    assert first["status"] == "SUCCESS"
    assert second["idempotent_replay"] is True
    connection = connect()
    assert connection.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 1
    connection.close()


def test_acknowledgment_message_and_status():
    from controls import execute
    from database import connect

    token = prepare_action("ACKNOWLEDGE", {})
    execute("ACKNOWLEDGE", "BC-7001", "G", "AUTH", "ACK", {}, token)
    connection = connect()
    message = connection.execute("SELECT message FROM communications").fetchone()[0]
    ack_status = connection.execute(
        "SELECT ack_status FROM claims WHERE claim_id='BC-7001'"
    ).fetchone()[0]
    connection.close()
    assert message == "We received your baggage claim and will keep you updated."
    assert ack_status == "QUEUED"


def test_courtesy_success_replay_and_persistence():
    from controls import execute
    from database import connect, tx

    token = prepare_action("COURTESY_RESOLUTION", {"record_id": "REC", "verified_discrepancy": "25", "currency": "USD"})
    with tx() as connection:
        connection.execute(
            """INSERT INTO compensation_records(
                record_id,passenger_name,claim_id,approved_amount,paid_amount,
                verified_discrepancy,currency,record_status,hold_flags,
                prior_resolution_id,raw_input,validation_errors
            ) VALUES('REC','Ada','BC-7001','100','75','25','USD','verified','[]',NULL,'{}','[]')"""
        )
    payload = {"record_id": "REC", "verified_discrepancy": "25", "currency": "USD"}
    first = execute(
        "COURTESY_RESOLUTION", "BC-7001", "G", "AUTH", "COURTESY", payload, token
    )
    second = execute(
        "COURTESY_RESOLUTION", "BC-7001", "G", "AUTH", "COURTESY", payload, token
    )
    connection = connect()
    assert connection.execute("SELECT COUNT(*) FROM courtesy_resolutions").fetchone()[0] == 1
    connection.close()
    assert first["status"] == "SUCCESS"
    assert second["idempotent_replay"] is True


def test_payload_hash_conflict():
    from controls import execute

    token = prepare_action("ROUTE", {"destination_team": "A"})
    execute("ROUTE", "BC-7001", "G", "AUTH", "SAME", {"destination_team": "A"}, token)
    with pytest.raises(ValueError, match="IDEMPOTENCY_CONFLICT"):
        execute(
            "ROUTE", "BC-7001", "G", "AUTH", "SAME", {"destination_team": "B"}, token
        )


@pytest.mark.parametrize(
    "human_action,expected",
    [("HUMAN_CLOSE", "closed"), ("HUMAN_REOPEN", "open")],
)
def test_human_close_and_reopen(human_action, expected):
    from controls import save_override
    from database import connect, tx

    add()
    with tx() as connection:
        connection.execute(
            "INSERT INTO graph_runs(graph_run_id,claim_id,claim_version,status,trigger_type,decision) VALUES('G','BC-7001',1,'HUMAN_REVIEW','TEST','{}')"
        )
    save_override("BC-7001", "G", "Reviewer", {}, human_action, "Reviewed")
    connection = connect()
    assert connection.execute("SELECT claim_status FROM claims").fetchone()[0] == expected
    connection.close()


def test_lost_lease_cannot_finalize():
    from database import LeaseOwnershipLost, acquire, set_status, tx

    add()
    claim = acquire(limit=1)[0]
    with tx() as connection:
        connection.execute(
            "UPDATE claims SET processing_token='NEW-WORKER' WHERE claim_id='BC-7001'"
        )
    with pytest.raises(LeaseOwnershipLost):
        set_status("BC-7001", "PROCESSED", processing_token=claim["processing_token"])


def test_rejected_node_is_persisted():
    from database import connect, tx
    from graph import Graph

    add()
    claim = {"claim_id": "BC-7001", "version": 1, "processing_token": None}
    graph = Graph(claim)
    with tx() as connection:
        connection.execute(
            "INSERT INTO graph_runs(graph_run_id,claim_id,claim_version,status,trigger_type) VALUES(?,'BC-7001',1,'RUNNING','TEST')",
            (graph.gid,),
        )
    late = result(
        "rules",
        {},
        gid=graph.gid,
        completed=datetime.now(timezone.utc) + timedelta(seconds=20),
        deadline=datetime.now(timezone.utc),
    )
    graph.consume(late)
    connection = connect()
    row = connection.execute("SELECT status,rejection_reason FROM node_runs").fetchone()
    connection.close()
    assert tuple(row) == ("REJECTED", "NODE_DEADLINE_EXCEEDED")


def test_semantic_conflict_can_be_resolved():
    from reducers import reduce_results

    rows = [
        result("rules", {"primary_category": "DB"}, run="1"),
        result("triage_agent", {"category": "LP"}, run="2"),
        result("reassessment_agent", {"category": "LP", "confidence": 96}, run="3"),
    ]
    facts = reduce_results(rows)
    assert not facts["conflicts"]["unresolved"]
    assert facts["conflicts"]["resolved"]["category"]["resolution"] == "LP"


def test_existing_database_additive_migration(monkeypatch, tmp_path):
    import sqlite3
    import config
    import database

    path = tmp_path / "old.db"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE claims(claim_id TEXT PRIMARY KEY,passenger_name TEXT,body_text TEXT,raw_input TEXT,input_hash TEXT,ingestion_status TEXT,validation_errors TEXT,version INTEGER,automation_status TEXT,claim_status TEXT)"
    )
    connection.commit()
    connection.close()
    monkeypatch.setattr(config.SETTINGS, "db_path", str(path))
    monkeypatch.setattr(database.SETTINGS, "db_path", str(path))
    database.init_db()
    connection = database.connect()
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(claims)")}
    connection.close()
    assert {
        "processing_token",
        "lease_expires_at",
        "attempt_count",
        "passenger_status",
        "created_at",
        "updated_at",
    } <= columns


def test_empty_conflict_container_does_not_block_route():
    from controls import authorize
    from database import tx

    add()
    with tx() as connection:
        connection.execute(
            "INSERT INTO graph_runs(graph_run_id,claim_id,claim_version,status,trigger_type) VALUES('G','BC-7001',1,'RUNNING','TEST')"
        )
    facts = {
        "conflicts": {"unresolved": {}, "resolved": {}},
        "final_category": "DB",
        "destination_team": "Baggage Tracing",
    }
    output = authorize(
        "ROUTE",
        {"claim_id": "BC-7001"},
        facts,
        "G",
        {"courtesy_limit": "250.00"},
        {"destination_team": "Baggage Tracing"},
    )
    assert output["authorized"] is True


def test_action_is_not_executed_if_action_step_cannot_be_reserved(monkeypatch):
    import config
    import graph
    from database import acquire, connect

    add()
    monkeypatch.setattr(graph, "invoke", fake_llm)
    monkeypatch.setattr(config.SETTINGS, "max_steps", 4)
    with pytest.raises(RuntimeError, match="GRAPH_STEP_LIMIT_EXCEEDED"):
        graph.run_agent(acquire(limit=1)[0])
    connection = connect()
    assert connection.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0
    connection.close()


def test_rejected_policy_result_is_not_used(monkeypatch):
    import graph
    from database import acquire, connect

    add()
    monkeypatch.setattr(graph, "invoke", fake_llm)
    monkeypatch.setattr(graph.SETTINGS, "node_deadline_seconds", 0)
    output = graph.run_agent(acquire(limit=1)[0])
    assert output["status"] == "HUMAN_REVIEW"
    assert output["policy"]["authorized"] is False
    connection = connect()
    assert connection.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0
    assert all(
        row[0] == 0
        for row in connection.execute(
            "SELECT authorized FROM policy_authorizations WHERE graph_run_id=?",
            (output["graph_run_id"],),
        )
    )
    connection.close()


def test_token_budget_blocks_second_agent(monkeypatch):
    import graph
    from database import acquire

    def costly(role, text, context=None):
        metadata = {
            "cache_hit": False,
            "provider_requests": 1,
            "provider_retries": 0,
            "schema_repairs": 0,
        }
        if role == "triage":
            return (
                {
                    "category": "DB",
                    "confidence": 50,
                    "urgency": "H",
                    "secondary": ["LP"],
                    "needs": ["CTX"],
                    "ambiguous": True,
                    "disposition": "CLAIM",
                    "reason_codes": ["AMB"],
                    "rationale": "Ambiguous.",
                },
                900,
                600,
                1,
                metadata,
            )
        return fake_llm(role, text, context)

    add()
    monkeypatch.setattr(graph, "invoke", costly)
    with pytest.raises(RuntimeError, match="CLAIM_TOKEN_BUDGET_EXCEEDED"):
        graph.run_agent(acquire(limit=1)[0])


def test_authorized_payload_hash_blocks_first_modified_execution():
    from controls import authorize, execute
    from database import acquire, tx

    add()
    claim = acquire(["BC-7001"], 1)[0]
    with tx() as connection:
        connection.execute(
            "INSERT INTO graph_runs(graph_run_id,claim_id,claim_version,status,trigger_type) VALUES('G','BC-7001',1,'RUNNING','TEST')"
        )
    facts = {
        "conflicts": {"unresolved": {}, "resolved": {}},
        "final_category": "DB",
        "destination_team": "Baggage Tracing",
    }
    approved = {"destination_team": "Baggage Tracing"}
    authorization = authorize(
        "ROUTE", {"claim_id": "BC-7001"}, facts, "G", {}, approved
    )
    with pytest.raises(PermissionError, match="AUTHORIZED_PAYLOAD_MISMATCH"):
        execute(
            "ROUTE",
            "BC-7001",
            "G",
            authorization["authorization_id"],
            "PAYLOAD-BOUND",
            {"destination_team": "Other Team"},
            claim["processing_token"],
        )


def test_graph_persists_policy_snapshot(monkeypatch):
    import graph
    from database import acquire, connect

    add()
    monkeypatch.setattr(graph, "invoke", fake_llm)
    output = graph.run_agent(acquire(limit=1)[0])
    connection = connect()
    row = connection.execute(
        "SELECT policy_config_id,policy_snapshot FROM graph_runs WHERE graph_run_id=?",
        (output["graph_run_id"],),
    ).fetchone()
    connection.close()
    assert row["policy_config_id"]
    assert json.loads(row["policy_snapshot"])["routes"]["DB"] == output["decision"]["destination_team"]


def test_relationships_are_persisted(monkeypatch):
    import graph
    from database import acquire, connect

    add("BC-7101", "My bag never arrived at the carousel.")
    add("BC-7102", "My bag never arrived at the carousel.")
    monkeypatch.setattr(graph, "invoke", fake_llm)
    graph.run_agent(acquire(["BC-7102"], 1)[0], dry_run=True)
    connection = connect()
    count = connection.execute(
        "SELECT COUNT(*) FROM claim_relationships WHERE claim_id='BC-7102'"
    ).fetchone()[0]
    connection.close()
    assert count >= 1


def test_migrates_old_actions_and_classifications(monkeypatch, tmp_path):
    import sqlite3
    import config
    import database

    path = tmp_path / "legacy-full.db"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE actions(action_id TEXT PRIMARY KEY,claim_id TEXT,graph_run_id TEXT,action_type TEXT,idempotency_key TEXT UNIQUE)"
    )
    connection.execute(
        "CREATE TABLE classifications(classification_id TEXT PRIMARY KEY,graph_run_id TEXT,claim_id TEXT,category TEXT)"
    )
    connection.commit()
    connection.close()
    monkeypatch.setattr(config.SETTINGS, "db_path", str(path))
    monkeypatch.setattr(database.SETTINGS, "db_path", str(path))
    database.init_db()
    connection = database.connect()
    action_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(actions)")
    }
    classification_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(classifications)")
    }
    connection.close()
    assert {"actor_type", "payload_hash", "policy_authorization_id", "request_payload"} <= action_columns
    assert {"phase", "agreement", "model_id", "prompt_version"} <= classification_columns


def test_dry_run_authorization_is_not_executable(monkeypatch):
    import graph
    from database import acquire, connect

    add()
    monkeypatch.setattr(graph, "invoke", fake_llm)
    output = graph.run_agent(acquire(limit=1)[0], dry_run=True)
    connection = connect()
    row = connection.execute(
        "SELECT authorized,authorization_mode FROM policy_authorizations WHERE graph_run_id=?",
        (output["graph_run_id"],),
    ).fetchone()
    action_count = connection.execute("SELECT COUNT(*) FROM actions").fetchone()[0]
    connection.close()
    assert tuple(row) == (0, "SIMULATION")
    assert action_count == 0


def test_authorization_is_graph_bound_and_single_use():
    from controls import authorize, execute
    from database import acquire, tx

    add()
    claim = acquire(limit=1)[0]
    with tx() as connection:
        connection.execute(
            "INSERT INTO graph_runs(graph_run_id,claim_id,claim_version,status,trigger_type) VALUES('G1','BC-7001',1,'RUNNING','TEST')"
        )
        connection.execute(
            "INSERT INTO graph_runs(graph_run_id,claim_id,claim_version,status,trigger_type) VALUES('G2','BC-7001',1,'RUNNING','TEST')"
        )
    facts = {
        "conflicts": {"unresolved": {}, "resolved": {}},
        "final_category": "DB",
        "destination_team": "Baggage Tracing",
    }
    payload = {"destination_team": "Baggage Tracing"}
    authorization = authorize("ROUTE", {"claim_id": "BC-7001"}, facts, "G1", {}, payload)
    with pytest.raises(PermissionError):
        execute(
            "ROUTE", "BC-7001", "G2", authorization["authorization_id"], "WRONG-GRAPH", payload, claim["processing_token"]
        )
    execute(
        "ROUTE", "BC-7001", "G1", authorization["authorization_id"], "FIRST", payload, claim["processing_token"]
    )
    with pytest.raises(PermissionError):
        execute(
            "ROUTE", "BC-7001", "G1", authorization["authorization_id"], "SECOND", payload, claim["processing_token"]
        )


def test_stale_worker_cannot_execute_action():
    from controls import authorize, execute
    from database import acquire, tx

    add()
    claim = acquire(limit=1)[0]
    with tx() as connection:
        connection.execute(
            "INSERT INTO graph_runs(graph_run_id,claim_id,claim_version,status,trigger_type) VALUES('G','BC-7001',1,'RUNNING','TEST')"
        )
    facts = {
        "conflicts": {"unresolved": {}, "resolved": {}},
        "final_category": "DB",
        "destination_team": "Baggage Tracing",
    }
    payload = {"destination_team": "Baggage Tracing"}
    authorization = authorize("ROUTE", {"claim_id": "BC-7001"}, facts, "G", {}, payload)
    with tx() as connection:
        connection.execute(
            "UPDATE claims SET processing_token='NEW-WORKER' WHERE claim_id='BC-7001'"
        )
    with pytest.raises(PermissionError, match="PROCESSING_LEASE_OWNERSHIP_LOST"):
        execute(
            "ROUTE", "BC-7001", "G", authorization["authorization_id"], "STALE", payload, claim["processing_token"]
        )
