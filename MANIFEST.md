# MANIFEST.md

# BagOps Technical Manifest

## Agent Logic

### Rules Agent
Implementation: rules_tools.py
Type: Deterministic
Responsibilities: category detection, urgency detection, safety analysis, prompt injection discovery, administrative detection, criticality assessment.
Outputs: category, urgency, confidence, reason codes, critical flags.

### Triage Agent
Implementation: llm_client.py, schemas.py
Type: LLM
Responsibilities: semantic classification, confidence estimation, ambiguity analysis, evidence planning, disposition selection.
Outputs: category, confidence, urgency, evidence requirements and rationale.

### Reassessment Agent
Implementation: llm_client.py
Type: LLM
Responsibilities: evidence reconciliation, conflict resolution, final recommendation generation.
Outputs: category, action, urgency, confidence and rationale.

### Policy Gate
Implementation: controls.py, policy_store.py
Type: Deterministic authorization controller.
Responsibilities: validate actions, enforce guardrails, generate authorization artifacts.

### Human Review
Implementation: controls.py, app.py
Responsibilities: review uncertain outcomes and persist override decisions.

## Orchestration Logic

Implementation Files:
- graph.py
- reducers.py
- processor.py

Execution Model:
- graph-based
- parallel execution
- conditional scheduling
- deterministic state reduction

Execution Sequence:
1. Rules Agent
2. History Tool
3. Triage Agent
4. Reducer
5. Conditional Evidence Tools
6. Reducer
7. Reassessment Agent
8. Policy Gate
9. Action or Human Review
10. Audit Recording

Reducer Responsibilities:
- accept results
- reject stale outputs
- calculate agreement
- preserve conflicts
- maintain graph state

Result Rejection Conditions:
- stale graph run
- stale claim version
- duplicate run id
- deadline exceeded
- terminal graph state

Runtime Controls:
- max_steps
- max_llm_calls
- max_tool_calls
- max_tokens
- processing leases
- retries
- deadlines

## Tool Definitions

### history_tool
Purpose: duplicate and relationship discovery.
Returns: duplicate candidate, canonical claim, evidence and related claims.

### context_tool
Purpose: operational context retrieval.
Returns: status, assigned team and previous actions.

### tracking_tool
Purpose: baggage movement reconstruction.
Returns: handover, loading, transfer and delivery events.

### compensation_tool
Purpose: financial verification.
Returns: verified discrepancy, currency, hold flags, record status and prior resolutions.

## Persistence Code

Implementation: database.py
Database: SQLite

Claims Domain:
- claims

Execution Domain:
- graph_runs
- node_runs
- classifications

Authorization Domain:
- policy_authorizations
- configuration_versions

Actions Domain:
- actions
- communications
- courtesy_resolutions

Relationship Domain:
- claim_relationships

Human Review Domain:
- human_overrides

Audit Domain:
- audit_events

Cache Domain:
- llm_cache

Testing Domain:
- regression_cases

Persistence Features:
- WAL mode
- transactions
- foreign keys
- additive migrations
- replay protection
- idempotency tracking
- audit history

## File Responsibilities
app.py : user interface
graph.py : orchestration engine
reducers.py : state reduction
controls.py : authorization and execution
database.py : persistence
rules_tools.py : rules and tools
llm_client.py : model connectivity
policy_store.py : policy management
schemas.py : structured contracts
processor.py : batch automation
config.py : runtime settings
seed.py : data loading
test_bagops.py : regression validation

## Rationale

I designed BagOps as a graph-based agent rather than a single LLM call or a fixed sequential chain. Every valid claim is classified by a specialized Triage Agent, while deterministic rules independently detect urgent items, tampering, prompt-injection attempts, and category signals. The graph then selects only the read-only evidence tools required for the claim, including history, tracking, context, and compensation records. When the available signals disagree, a separate Reassessment Agent evaluates a compact, source-labelled evidence package. The LLM can recommend a category or action, but it cannot access mutation tools. Routing and other actions require deterministic, persisted policy authorization.

Where the requirements were underspecified, I chose conservative behavior. Critical claims, unresolved conflicts, failed safety checks, unavailable mandatory history, failed requested tools, stale or late evidence, expired leases, and policy denials all result in human review. Semantic category disagreements may be resolved by high-confidence reassessment, but identity, currency, safety, and financial conflicts cannot be overridden by the model. Dry runs create non-executable simulation authorizations, while live authorizations are bound to a specific graph run, claim, action, and payload. They are also single-use and protected by idempotency and processing-lease checks.

I used AI assistance for design review, test generation, and identifying edge cases, but I did not accept its suggestions without verification. I corrected several initially suggested implementations, including treating an empty conflict structure as a real conflict, executing mutations before reserving the graph step, relying on the last graph result as the policy decision, allowing reusable authorizations, and checking worker ownership only after mutation. I also retained strict human review when mandatory evidence fails, rather than allowing an optimistic rules or model fallback.

With more time, I would add enterprise authentication and role-based access, move from additive SQLite migrations to versioned database migrations, introduce managed queues and observability, strengthen passenger identity matching, and integrate real airline tracking, communication, and payment services. I would also conduct load, security, privacy, fairness, and failure-recovery testing before any production deployment.
