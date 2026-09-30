---
name: code-structure-service-layer
description: "Service-layer architecture separating orchestration/actions (business rules, auth, status transitions) from service mechanics (reusable operations, provider calls, structured results). Use when refactoring repeated operational blocks or adding features sharing mechanics."
---

# Service Layer Architecture

Enforce a strict two-layer separation: **Actions** orchestrate domain policies and product rules (the "why/when"), while a **Service Layer** centralizes reusable operational mechanics (the "how").

This prevents duplicate logic, inconsistent behavior across endpoints, and bugs patched in one workflow but neglected in others.

## When to Use

- Multiple callers execute the same operational logic (e.g. order submission, settlement processing, market queries, external webhook handling).
- Copy-pasted database queries or provider SDK calls appear across action routes.
- A bug fix in one controller fails to propagate to another endpoint performing the same task.
- Adding a new feature that shares operational mechanics with existing capabilities.

**Do NOT use when:** Logic is genuinely unique to a single caller and has no reusable operational components (avoid premature over-abstraction).

## Core Architecture Pattern

```
Orchestration Layer (Actions / Routers)     Service Layer (Shared Mechanics)
├── Owns HTTP routing & request parsing     ├── Owns reusable operational steps
├── Owns authentication & authorization     ├── Owns provider/SDK integrations (Kalshi, Stripe)
├── Owns domain invariants & policy checks  ├── Owns DB query composition & Redis pipelines
├── Owns state machine transitions          ├── Accepts explicit typed parameters
├── Owns error classification & HTTP status ├── Returns structured results (Pydantic / dataclasses)
└── Coordinates calls to services           └── NEVER bypasses domain invariants
```

### Rule of Thumb
- **"What does this product action mean, and is the actor permitted to do it?"** -> Keep in the Action / Router.
- **"How is this low-level operation executed reliably and efficiently?"** -> Move to the Service Layer.

## Repository Invariants & Safety Rules

1. **Atomic Ledger Integrity:**
   - Balance changes must **never** be performed via direct ad-hoc column updates (`wallet.balance += amount`) in a service.
   - Always route balance mutations through designated wallet helpers (`wallets/`) that atomically produce paired `ledger_entries` records.
2. **Database Engine Separation:**
   - FastAPI HTTP routes use the API connection pool (`DATABASE_URL`, direct port 5432 session mode on staging).
   - Long-running daemons and background tasks use `DATABASE_DIRECT_URL`.
   - Never mix connection lifecycles across boundaries.
3. **Redis & Cache Batching:**
   - Never issue sequential Redis reads in a loop when `MGET` or a pipeline can batch them.
   - Warm cache hits must perform zero redundant DB checkouts or queries.
4. **Additive and Reversible Contracts:**
   - Keep schema and API contracts backward- and forward-compatible.
   - Do not break existing callers when introducing new service methods.
5. **Clean Cutover Without Shims:**
   - When migrating callers to a service layer, migrate all callers and delete dead code, obsolete shims, or aliases completely.

## Designing Composable Capability Blocks

Design services as focused, composable capability blocks rather than monolithic "do everything" functions:

```python
# Good: Composable, granular capabilities with explicit parameters
async def get_market_quote(market_id: str) -> MarketQuote: ...
async def reserve_order_funds(wallet_id: UUID, amount: Decimal) -> ReservationResult: ...
async def submit_limit_order(params: OrderPlacementParams) -> OrderRecord: ...
async def notify_order_filled(order_id: UUID) -> None: ...
```

Each service function MUST:
- Accept all required context as **explicit parameters** (no hidden global state or thread-locals).
- Return **structured types** (Pydantic models or typed dataclasses).
- Avoid swallowing errors; raise typed domain exceptions or return structured failure results.
- Keep transaction boundaries clear and explicit.

## Migration & Refactoring Checklist

When extracting shared mechanics:

1. **Document Existing Behavior:** Map the existing caller implementations and identify identical operational chunks.
2. **Design the Service Signature:** Define explicit parameter types and structured return models.
3. **Extract Mechanics First:** Move non-domain operations (query building, external API interaction, serialization) into the service module.
4. **Migrate One Caller:** Update a single action caller, run targeted verification, and inspect response behavior.
5. **Migrate Remaining Callers:** Switch all callers to the unified service function.
6. **Clean Cutover:** Remove duplicate code, dead helper functions, and unused imports. Do not leave deprecated shims behind.
7. **Targeted Verification:** Run focused typechecks and targeted unit tests (e.g. `npx tsc --noEmit` or `pytest tests/test_service.py`). Skip project-wide test suites.

## Anti-Patterns

| Anti-Pattern | Description | Fix |
|---|---|---|
| **God Service** | A single massive service class that handles auth, queries, billing, and emails. | Decompose into domain-focused services (`order_service.py`, `market_service.py`). |
| **Leaky Service** | A service function that mutates domain state directly without enforcing business invariants. | Enforce preconditions in action layer; require atomic ledger operations in wallet services. |
| **Silent Swallower** | A service catching all exceptions and returning `None` or `{}`. | Raise typed domain exceptions or return explicit `{ success: False, error: str }`. |
| **Over-Abstraction** | Creating five wrapper layers and interfaces for a single 5-line database query used in one place. | Keep simple, single-use logic in the action layer until true duplication occurs. |
