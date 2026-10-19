# Setu Account Aggregator Sandbox Integration Plan

## Scope and safety

Implement the Setu FIU sandbox flow only. All Setu routes and storage must use
`database.get_test_engine()` and verify `SELECT current_database()` is exactly
`BFAMS-DeFI-test` before each write. Setu sandbox data will use a new
`setu_sandbox` provider, a new sandbox user, and a connection whose external ID
starts with `SETU-SANDBOX-`; it must never share the Dhan or seed users or
connections. No schema changes or production credentials are in scope.

The implementation must not inspect `.env`, `data/`, `data_private/`, or
`data/user_holdings/`. Setu configuration will be checked only by reporting
boolean presence for the four approved setting names. Secrets, tokens,
authorization headers, account identifiers, and financial payloads must not
be logged or returned.

## Existing architecture

- `database.py` defines the main engine and `get_test_engine()`. Setu-specific
  code must use only the latter.
- `app/schemas.py` provides `NormalizedAccount` and `NormalizedTransaction`.
  Account IDs are required; transaction IDs are required; transaction amounts
  must be positive; transaction timestamps must be timezone-aware.
- `app/ingestion.py` validates normalized accounts and transactions, including
  duplicate transaction IDs within a batch.
- `app/store.py` stores accounts, transactions, and a `DataSyncRun`, skipping
  existing accounts and transactions.
- `app/models.py` has users, providers, connections, accounts, transactions,
  sync runs, and an ORM mapping for the existing `consent_records` table.
- Existing database dependencies include a main-database dependency and a
  test-database dependency. New Setu routes need a dedicated safe dependency
  that constructs its session from `get_test_engine()` and verifies the test
  database before writes.
- Setu integration package/client and consent/session routes have been added
  in the current implementation phases.
- Existing storage has no normalized investment-holdings path. The requested
  initial data scope (securities/equity holdings) is not represented by
  `NormalizedAccount` / `NormalizedTransaction` or `store_sync()`.

## Planned files

### Add

- `app/integrations/__init__.py`
- `app/integrations/setu/__init__.py`
- `app/integrations/setu/config.py` — optional Setu settings; sandbox default.
- `app/integrations/setu/exceptions.py` — safe provider/configuration errors.
- `app/integrations/setu/client.py` — documented Setu HTTP contract only.
- `app/integrations/setu/parser.py` — validate documented response structures.
- `app/integrations/setu/normalizer.py` — map supported records to existing
  normalized schemas; reject unstable IDs, invalid amounts, and naive times.
- `app/integrations/setu/service.py` — consent, status, session, retrieval, and
  orchestration.
- A focused Setu route module and mocked Setu tests, with exact filenames chosen
  after checking the existing route/test conventions in the implementation
  phases.
- `docs/setu_integration_plan.md` — this document.
- `scripts/setu_sandbox_setup.py` — preview or create the isolated sandbox
  provider, user, and connection in the TEST database.

### Modify

- `main.py` — register the Setu router without changing existing routes.
- `app/dependencies.py` only if a dedicated test-engine session dependency is
  needed; it must not use `get_db` or the main engine for Setu.
- `app/store.py` only if a narrowly scoped safety guard is needed to guarantee
  the Setu call to the existing store pipeline cannot target another database.
- `.env.example` only if it already exists; add empty placeholders, never
  secrets.
- Relevant test modules only; do not run Dhan/MF ingestion, `test_store.py`, or
  `test_store_failure.py`.

No changes to `database.py`'s existing `Settings` class are planned.

## Setu workflow

1. Load optional Setu settings; app startup remains successful when credentials
   are absent.
2. Permit the unauthenticated development routes only when the configured
   environment is exactly `sandbox`.
3. Use the documented Setu authentication and connectivity contract.
4. Create a consent request using the documented consent payload and preserve
   the actual provider consent reference.
5. Return only the documented consent URL/reference needed by the UI; poll the
   documented consent status.
6. Create a data session only after provider-reported approval.
7. Fetch only financial information types documented and enabled in the
   sandbox; do not fabricate unsupported holdings.
8. Parse and normalize records; reject missing external IDs, invalid amounts,
   unsupported directions, malformed values, and timezone-naive timestamps.
9. Send normalized accounts and transactions through `store_sync()` on the
   isolated Setu connection. Skip existing provider records; do not update or
   delete rows.
10. Record sync state with the existing `DataSyncRun` columns and expose only
    safe statuses/counts.

Consent and data-session request paths and payloads have been checked against
the official Setu docs and FIU V2 Postman collection.

## Database mapping and sandbox isolation

- `DataProvider`: create or reuse `provider_code='setu_sandbox'`.
- `User`: create one new sandbox user, distinct from all existing users.
- `FinancialConnection`: create/reuse only a Setu sandbox connection, with
  `external_connection_id` beginning `SETU-SANDBOX-`.
- `FinancialAccount` and `Transaction`: insert through `store_sync()` using
  provider-stable external IDs.
- `DataSyncRun`: use existing connection, sync type, status, timestamps,
  `records_processed`, and `error_message`.
- `consent_records` has an ORM mapping for the existing table, but the current
  consent creation and polling endpoints do not store or update consent rows.
  Consent creation is restricted to a `setu_sandbox` connection with an
  external ID beginning `SETU-SANDBOX-`. The request accepts an explicit VUA
  and validity duration, optionally a timezone-aware data range; the VUA is
  sent to Setu but not stored. Creation returns the provider's consent ID,
  status, and validated HTTPS Setu approval URL. Product-level consent purpose,
  FI types, consent mode, fetch type, data life, frequency, and redirect
  settings must be configured and verified in Setu Bridge rather than
  silently chosen by the application.
- Consent status polling calls the documented Get Consent API and returns the
  provider's consent ID and status without local persistence. The consent
  reference is supplied by the caller; no local consent lookup is required.
- Data session creation uses `POST /v2/sessions` only after a fresh Get Consent
  response reports `ACTIVE`. The request requires an explicit `consentId`,
  timezone-aware `dataRange`, and `format` (`json` or `xml`); Setu requires the
  requested range to be within the granted consent. The response returns the
  session ID, consent ID, format, and status to the caller. Session creation
  does not persist anything locally.
- `GET /api/setu/sessions/{sessionId}` retrieves a session response without
  altering it. Add `?poll=true` to poll until `PARTIAL` or `COMPLETED`; `PENDING`
  is never returned as ready, and `EXPIRED` / `FAILED` are terminal errors.
  Polling is bounded by `SETU_SESSION_POLL_INTERVAL_SECONDS`,
  `SETU_SESSION_POLL_MAX_ATTEMPTS`, and `SETU_SESSION_POLL_TIMEOUT_SECONDS`.
  The route does not persist, parse, normalize, or log the financial payload.
- Consent and session API requests use Setu's versioned `/v2` paths, verified
  against Setu's [official FIU V2 Postman collection](https://documenter.getpostman.com/view/22511424/2s9Y5VU4MC).
  FI fetch initiation is a separate explicit `POST /v2/sessions/{id}`; it does
  not return or persist financial data. Retrieval first checks
  `GET /v2/consents/{consentId}/fetch/status` and is allowed only for
  provider-reported `PARTIAL` or `COMPLETED`, then reads
  `GET /v2/sessions/{id}`. The raw session response is returned by the
  retrieval endpoint; it is neither stored nor logged.
- FIU V2 API calls obtain a bearer token from
  `POST https://orgservice-prod.setu.co/v1/users/login` using the documented
  `client: bridge` header and client-credentials JSON body. Subsequent FIU
  requests use `Authorization: Bearer` and `x-product-instance-id`; `GET
  /v2/fips` is a route-specific exception using `x-client-id` and
  `x-client-secret`. Token expiry is unspecified by the reviewed official
  sources, so no TTL is assumed.
- FI data normalization currently accepts stable deposit-account references
  and credit/debit transactions from JSON responses. Other FI types (including
  equity and mutual-fund holdings) are rejected as unsupported; they cannot be
  represented by the current normalized storage pipeline.
- Explicit `POST /api/setu/consents/{consentId}/sessions/{sessionId}/sync`
  retrieves normalized supported records only after consent and fetch status
  are revalidated, checks `current_database()` immediately before persistence,
  then delegates inserts and sync-run recording to `store_sync()` on the
  `get_test_engine()` session. `store_sync()` commits internally. Existing
  account/transaction duplicates are skipped; no existing records are updated
  or deleted. The response contains counts and rejection reason counts only.
- `DataSyncRun` has no created/updated counts or session ID. These would improve
  observability, but must not be added without schema approval.
- `account_balances` and `investment_holdings` have no database unique
  constraint. Do not add constraints. Application-level duplicate handling is
  required where those records are supported.
- Current normalized storage does not persist securities/equity holdings.
  Before selecting that scope, identify whether the existing schema and store
  can represent Setu's documented data without bypassing `store_sync()`; if
  not, present the exact schema SQL needed and wait for `approved schema`.

## Testing strategy

- Use synthetic Setu payloads in tests and mock all HTTP calls.
- Test optional configuration, missing/invalid configuration, client
  initialization, authentication/request shape, timeouts, HTTP errors,
  malformed responses, consent creation/status, session creation, and data
  retrieval.
- Test parsing and normalization for stable IDs, positive amounts, credit /
  debit direction, timezone-aware timestamps, and rejection behavior.
- Test duplicate handling, Setu-only provider/user/connection isolation,
  `store_sync()` integration, and sandbox-only route protection.
- Database tests must use only `database.get_test_engine()`, assert
  `current_database()` before writes, and clean up only records created by the
  test if cleanup is necessary; never alter seed, Dhan, MF, or unrelated rows.
- Do not run Dhan ingestion, MF ingestion, `testing/test_store.py`, or
  `testing/test_store_failure.py`.
- Regression checks are limited to importing application modules, starting the
  FastAPI app, `GET /health`, and `GET /docs`.
- Do not perform real Setu calls during routine tests. A real sandbox flow is a
  separate final phase requiring configured credentials and user approval in
  the provider UI.

## Known limitations and production requirements

- No Setu FIU onboarding artifacts or verified API contract were available at
  Phase 1. Verify current official documentation before client implementation.
- A sandbox provider row is not a working integration.
- The existing data model/store supports accounts and transactions but not
  investments/holdings. The user's requested securities/equity holdings scope
  may require explicit schema approval or a later supported data-scope choice.
- `consent_records` maps the existing consent table; the consent creation and
  polling endpoints do not persist consent records.
- `scripts/setu_sandbox_setup.py` is dry-run by default. Use `--apply` only
  after explicit approval to create the sandbox provider, user, and connection.
- Unauthenticated sandbox routes are acceptable only for local development;
  production requires authentication, authorization, CSRF protections where
  applicable, secure callback handling, secret management, consent lifecycle
  controls, audit logging, and provider compliance review.
- Scheduled refresh must respect provider consent, expiry, revocation, and
  frequency rules; no background scheduler is planned until the provider
  contract is verified.

## Phase gates

Implementation follows the supplied 14-phase sequence. After each phase,
report only changed files, tests, pass/fail, next step, and wait for the exact
`continue` response. Stop for exact `approved schema` before any schema change.
