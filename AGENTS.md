# AGENTS.md

# BagOps Agent Architecture Specification

## System Purpose
BagOps is a graph-orchestrated baggage claims platform that separates reasoning, evidence retrieval, authorization, execution, and human review. The architecture is intentionally designed so that language model outputs are recommendations and never direct actions.

## Architectural Principles
1. Graph over chain execution.
2. Parallel independent node execution.
3. Evidence before final decisions.
4. Deterministic policy authorization.
5. Human review for uncertainty.
6. Full auditability.
7. Idempotent actions.
8. Claim-bound and graph-bound authorization.

## Agent Inventory
### Rules Agent
Purpose: deterministic first-pass understanding.
Inputs: normalized claim text.
Outputs: category, urgency, criticality, tampering signals, prompt injection indicators, reason codes.

Detected Categories:
- DB Delayed Baggage
- DM Damaged Baggage
- MC Missing Contents
- LP Lost Property
- CR Compensation Request

Responsibilities:
- keyword classification
- urgency assignment
- safety detection
- critical item detection
- administrative request detection
- prompt injection detection

### Triage Agent
Purpose: semantic understanding of claim content.
Responsibilities:
- category classification
- confidence scoring
- ambiguity detection
- evidence planning
- disposition selection

Outputs:
- category
- confidence
- urgency
- secondary categories
- required evidence
- rationale

Evidence Requests:
- HIST
- CTX
- TRACK
- COMP

### Reassessment Agent
Purpose: re-evaluate using retrieved evidence.
Runs only after evidence collection.
Inputs include rule output, history, tracking, compensation, context, conflicts and agreement metrics.
Outputs final category, final urgency, recommendation and rationale.

Allowed Recommendations:
- ROUTE
- ACKNOWLEDGE
- REQUEST_INFORMATION
- CLOSE_DUPLICATE
- COURTESY_RESOLUTION
- HUMAN_REVIEW

### Policy Gate
Purpose: final authorization authority.
Validates every recommended action against policy and safeguards.
Produces authorization records and denial reasons.

### Human Review
Purpose: accountable oversight.
Triggered by critical claims, unresolved conflicts, failed verification, policy denial, tool failure, ambiguity or budget exhaustion.

## Execution Graph
Parallel Start:
- Rules Agent
- History Tool
- Triage Agent

Reducer

Conditional Tools:
- Context Tool
- Tracking Tool
- Compensation Tool

Reducer

Reassessment Agent

Policy Gate

Authorized Action OR Human Review

## Reducer Design
The reducer creates deterministic state from potentially unordered results. It tracks accepted evidence, rejected evidence, resolved conflicts, unresolved conflicts and agreement scores.

## Dynamic Scheduling
Tools are activated only when evidence is required. The graph is adaptive rather than fixed.

## Human Review Model
Human reviewers can route, acknowledge, request information, close, reopen or take no action. Overrides are stored permanently and audited.

## Safety Model
- Prompt injection detection
- Authorization binding
- Idempotency
- Lease ownership
- Audit logs
- Policy approval requirement
- Evidence verification before execution
