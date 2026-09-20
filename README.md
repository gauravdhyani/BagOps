# BagOps

This build implements the guarantees in the master plan: dynamic readiness-driven execution, append-only results, deterministic reducers, order-independent facts, result deduplication, stale/late rejection, selective tools, one optional reassessment, policy-gated simulated actions, leases, retries, idempotency, observability, human review and a Test Laboratory.

## Architecture

```text
Rules + Triage + History (parallel)
       ↓ accept/reject result
 deterministic reducer
       ↓ readiness check
 selected tools only
       ↓
 optional one reassessment
       ↓
 deterministic policy
   action or review
```

Every accepted result is checked for graph ID, claim version, duplicate run ID, terminal status and deadline. Safety is monotonic. Conflicts are preserved. Compensation tools outrank model extraction. The LLM never receives mutation tools.

## Run

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python seed.py
pytest -q
streamlit run app.py
```

If `bagops.db` is deleted, run `python seed.py` before processing.

## Provider examples

Groq:
```ini
BAG_LLM_PROVIDER=groq
BAG_LLM_URL=https://api.groq.com/openai/v1
BAG_LLM_MODEL=qwen/qwen3.8-27b
```

OpenAI or an OpenAI-compatible in-house endpoint uses the same `/v1` base URL pattern. `BAG_LLM_CA_BUNDLE` accepts a private CA certificate without disabling TLS verification.

## Safe fallback matrix

- LLM unavailable: per-run circuit opens; no later LLM calls; strict mode sends claim to review.
- History unavailable: duplicate closure blocked; reversible route can continue if verified.
- Tracking unavailable: only tracking-dependent outcomes blocked.
- Compensation unavailable: courtesy blocked.
- Policy/database failure: mutation blocked.
- Budget/deadline exhausted: graph finalized and human task created.

## Tests

The suite covers stale, late and duplicate result rejection; result-order permutations; monotonic safety; LLM outage circuit breaking; lease recovery; step exhaustion; prompt injection; compensation failures; and dry-run accounting.


## Production hardening

- Additive SQLite migrations upgrade supported older project databases at startup.
- Claim finalization requires the processing token that acquired the lease. A stale worker cannot overwrite a newer worker.
- Late node results are rejected and persisted as `REJECTED`; local functions are not forcibly terminated at the deadline.
- Acknowledgment and information-request messages use separate templates.
- Closed claims that would otherwise route are acknowledged instead.
- Semantic category disagreements may be marked resolved after a high-confidence reassessment; authoritative identity, currency, payment, safety, and financial conflicts remain unresolved and require review.
- Active policy routing is authoritative. A model-proposed destination team is ignored.
- `llm_calls` means logical agent invocations. Provider requests, provider retries, schema repairs, and cache hits are stored separately.
- Transport retries are owned by `llm_client.py`; the graph does not repeat agent nodes for the same transport failure.
