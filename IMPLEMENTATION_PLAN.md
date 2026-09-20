# IMPLEMENTATION_PLAN.md

# Project Blueprint

## Objective
Build a baggage claim automation platform with graph orchestration, evidence-driven reasoning, deterministic policy controls and complete auditability.

## Major Components
### Presentation Layer
Streamlit application providing dashboards, operations views, claim workbench, policy management, exports and audit review.

### Graph Orchestration Layer
Responsible for scheduling, state management, budget enforcement, retries, node acceptance, rejection handling and finalization.

### Agent Layer
Rules Agent, Triage Agent, Reassessment Agent, Policy Gate and Human Review.

### Tool Layer
History, Context, Tracking and Compensation verification tools.

### Persistence Layer
SQLite database with transactional operations and additive migrations.

## Database Design
Claims domain, graph execution domain, authorization domain, communications domain, audit domain, configuration domain, compensation domain, cache domain and regression testing domain.

### Key Tables
claims
graph_runs
node_runs
classifications
policy_authorizations
actions
communications
courtesy_resolutions
claim_relationships
human_overrides
audit_events
configuration_versions
llm_cache
regression_cases

## Runtime Flow
Ingest Claim -> Validate -> Parallel Graph Execution -> Evidence Collection -> Reassessment -> Policy Authorization -> Action or Human Review -> Audit

## State Management
PENDING -> PROCESSING -> PROCESSED
PENDING -> PROCESSING -> HUMAN_REVIEW -> HUMAN_DECIDED

## Policy System
Versioned policies define routing behavior, thresholds and operational settings while protected guardrails remain immutable.

## Security Controls
Prompt injection detection, payload-bound authorization, lease ownership, idempotency, audit logging, duplicate verification, compensation verification and policy-enforced actions.

## LLM Design
Structured schemas, deterministic temperatures, JSON responses, response validation, schema repair, caching and retry logic.

## Failure Handling
Node failures, stale results, token budget limits, deadline violations, authorization denial and compensation verification failures result in safe escalation.

## Deployment Design
Single Python application, SQLite persistence, environment-based configuration and OpenAI-compatible endpoint integration.

## Build Sequence
1. Configuration
2. Database
3. Rules Engine
4. Tool Layer
5. LLM Layer
6. Reducers
7. Graph Engine
8. Policy Engine
9. UI Layer
10. Tests
