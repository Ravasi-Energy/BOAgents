# BOAgents RC-01: installation and upgrade

This candidate uses `bo.execution-control.event.v1` with persistent checkpoint
`fencingToken`, plus the v1 mandate/status contract. The reviewed published
receiver is Guardian `e6bbf6c28d3dc18b8f2110f330ac6c8b8281f5b6` (including contract
7401eaa); the reviewed Hire producer is `1286fedc67fe7057b1f1c4c527c9ff2769404446`.
The event schema SHA256 is
`e04fafeba4a673eed51a01ce6c36269129e4c6c62981ce105f7f641735e75010`.
The final candidate SHA and all evidence hashes are in the RC-01 manifest
published by BO-SOL-02; this document is not a distributed-gate acceptance.

## Configure the existing installation

Use the existing runtimes/deployment layout described in [deployment.md](deployment.md).
No additional service is required. These are server process variables; they are
not frontend build variables or BO settings containing credentials.

| Variable | Process / exact purpose |
|---|---|
| `BACKEND_SHARED_SECRET` | API and UI, identical service-key value. API checks `x-api-key`. |
| `BACKEND_PROXY_SECRET` | API and UI, a second independently generated value. Never equal to the service key; never issued to service clients. |
| `AUTH_SECRET` | UI only; third independent secret for Auth.js sessions. |
| `AUTH_GOOGLE_ID`, `AUTH_GOOGLE_SECRET` | UI's existing OAuth application. Configure callback `<UI-origin>/api/auth/callback/google`. |
| `AUTH_URL`, `AUTH_TRUST_HOST` | Public UI origin and `true` behind the existing reverse proxy. |
| `ALLOWED_EMAILS` | UI login allowlist. Include the bootstrap administrator; does not itself grant BO admin. |
| `BO_ADMIN_EMAILS` | API BO administrator allowlist. Same chosen admin email as above; roster `is_principal` is an additional admin source. |
| `BACKEND_BASE_URL` | UI → API base; default `http://localhost:8000`, Compose uses `http://api:8000`. |
| `OE_PUBLIC_DEPLOYMENT` | API: `1` for an internet-reachable deployment. API startup refuses a missing shared secret. |
| `BO_TENANT_ID` | API, stable lowercase slug, default `local`; never selected by browser/request body. |
| `BO_TELEMETRY_PRODUCER_ID` | API, stable producer ID, default `boagents`; must match Guardian's credential. |
| `BO_INSTALLATION_ID` | API, stable installation ID, default `local-installation`; must match Guardian's credential and mandates. Product is fixed as `BOAgents` in code. |
| `BOAGENTS_DB_PATH` | API, absolute writable persistent path, e.g. `/data/bo_agents.db`. Preserve across upgrade. |
| `EPISODIC_DB_PATH` | API, separate absolute persistent audit path, e.g. `/data/episodic_memory.db`. Never rely on another checkout's cwd. |
| `BO_GUARDIAN_TOKEN` | API only. Default named credential for Guardian status/events; provision with `execobs:write` and the correct tenant/product/producer/installation identity. |
| `BO_GUARDIAN_POLICY_TOKEN` | Optional API-only credential with `execpolicy:read` for an additional direct policy read. Its absence does not bypass effective status BLOCKED. |

Generate three separate secret values once using the deployment's existing
secret mechanism; distribute the service and proxy values identically to their
two processes. Do not paste real values into examples, logs, PRs or settings.
Do not use `NEXT_PUBLIC_*` for any credential.

For an existing local checkout, `make dev` exports the root `.env` before
starting both processes. Plain Next.js startup reads `packages/ui/.env*`, not
the root `.env`; plain Python Settings dotenv loading does not export BO/auth
variables to `os.environ`. When using existing process launchers, inject/export
the variables explicitly. `make docker` passes the root env file to the existing
Compose layout, which passes the separate proxy credential to both services.
The Compose UI entrypoint is a development flow (`npm install`/dev), not the
production build used in FIN-01; RC-01 did not invoke it or install packages.

Configure the host application's existing provider/local-model settings as
required by its normal startup. RC/FIN probes used a synthetic lifespan and are
not evidence of a fully configured agent/provider deployment.

## Bootstrap operator and admin

1. Start API/UI with the variables above, keeping `bo.exec.enabled=false` until
   authority and data paths are checked. Use the existing public-deployment
   guard; do not relax authentication to bootstrap.
2. A service client sends only `x-api-key`. `GET /bo/settings` returns
   `role=operator`; `PUT /bo/settings/{key}` and mandate writes return 403.
   Operators can read and operate existing executions (pause/resume/cancel/work,
   reconcile/retry); they are not BO settings/mandate administrators.
3. Sign in through the UI as the email configured in both allowlists. Visit
   `/settings/bo`; it must show `rol: admin`. The session proxy strips incoming
   caller headers and adds its verified session email and proxy credential.
   A login-allowed user absent from BO_ADMIN_EMAILS/roster principals is a viewer.
4. Save settings in **Execuție și recuperare**. Every write uses the current
   per-setting `expected_version`; 409 means reload and review the latest value.
   Do not retry a stale overwrite blindly. The equivalent authenticated HTTP
   body is `{"value": <typed-value>, "expected_version": <current-version>}`
   at `PUT /bo/settings/{key}`. `GET /bo/settings` supplies versions and effect
   descriptions. No setting stores the token itself.

## Choose the execution authority mode

| Setting / condition | Standalone | Mandatory supervision |
|---|---|---|
| `bo.exec.guardian_auth_required` | `false` | `true` |
| Mandate `guardian_ref` and ancestors | All unbound | Bind to the matching Guardian mandate; every local ancestor constraint is checked |
| `bo.exec.guardian_endpoint` | May be empty | Guardian base URL, without `/v1/execution-events` suffix |
| `bo.exec.guardian_secret_ref` | Default `BO_GUARDIAN_TOKEN` unused for local-only checks | Name of the API env variable holding the token; default `BO_GUARDIAN_TOKEN` |
| `bo.exec.enabled` | Enable only after reviewing local limits | Enable last, after live authority checks |

A bound mandate is always supervised even if the global requirement is false.
A child inherits an omitted binding; a different explicit child binding adds a
constraint and does not replace the parent's. With mandatory supervision,
an unbound chain never produces an effect. Missing endpoint/credential or
unavailable Guardian causes a recoverable pause; configured endpoint with an
unbound required mandate is denied. No missing configuration silently converts
a bound mandate to standalone.

The explicit BO endpoint wins. If empty, `BO_TELEMETRY_ENDPOINT` is the fallback
from which the `/v1/...` suffix is stripped. The named token wins over
`BO_TELEMETRY_TOKEN`. Status is read at
`/v1/mandates/{ref}/status?product=BOAgents&installation=<id>&action=increment&resource=synth.counter`.
Execution evidence is always posted to that authority base's
`/v1/execution-events`, never to a generic observation URL. The independent
telemetry enable flag does not disable execution authorization.

Guardian's `allowed.actions` must contain exact `increment` and
`allowed.resources` exact `synth.counter` for the current synthetic provider.
`effect.intent` or a wildcard string is not a substitute for these exact
capabilities in the Guardian response. Local BO mandate patterns remain local
constraints. Set local mandate budgets, concurrency, expiry and max_steps to
the intended delegated limits; `bo.exec.max_steps` further caps new runs.
`BLOCKED` with `documentStatus=ACTIVE` is still a denial, including a revoked or
narrowed current policy. Legacy/incomplete status responses fail closed.

## Failures to verify before admitting traffic

| Trigger | Observable behavior |
|---|---|
| UI proxy secret absent or same as service key | Next.js 503 `delegated_identity_not_configured` |
| API proxy secret absent, same as service key, or mismatched | Delegated BO request 401 `untrusted delegated identity` |
| Wrong service key | API shared-secret gate 401 |
| Public API with missing service key | Startup failure, no open API |
| Service attempts settings/mandate write | 403; it cannot become admin by adding an email |
| Viewer spoofs caller headers toward UI proxy | Headers stripped; viewer remains viewer |
| Guardian non-ACTIVE, including BLOCKED | Effect denied; no provider dispatch |
| Bound/required authority unavailable | No new effect; recoverable pause |
| `bo.exec.enabled=false` | Submit 403, no claims; active work stops at next effect boundary |

## Guardian-first upgrade and conservative rollback

1. Stop existing producers/workers using their recorded process/service identity.
   Keep the configured tenant/producer/installation IDs, DB paths and credentials
   stable. Preserve existing databases, outbox envelopes, receipts and audit.
   Use the existing consistent-backup procedure; no live SQL edits or deletion.
2. Upgrade Guardian to the reviewed contract or a separately reviewed compatible
   successor. Confirm its credential identity/scope and status endpoint first.
3. Upgrade BOAgents, provision the separate proxy credential, and restart both
   API and UI with the same stable identities. Startup's additive migration
   restores conservative exposure for legacy RELEASED ambiguous ledger entries
   and keeps active reservations. Old persisted envelopes are not rebuilt.
4. Review UNKNOWN via receipt lookup/reconciliation. Resume retains run ID,
   idempotency key, payload digest and correlation; no blind resubmission.
   EXPOSED retains budget without active slots; resume reserves capacity
   atomically against every ancestor. RESERVED/EXPOSED/COMMITTED all consume
   cumulative budget. Confirmed/partially executed runs conservatively charge
   their declared run budget because the provider has no per-step charge.
5. RC-01 publishes the recovered receipt to the durable outbox. Its verdict time
   advances strictly for the same attempt, including a stationary clock, so
   Guardian can replace UNKNOWN with CONFIRMED. The original provider receipt
   and its timestamp remain in receipt JSON; the new time describes the recovered
   verdict, not a second provider execution.
6. Enable execution only after role/authority checks and review of unresolved
   operations. RC-01 does not deploy or claim a completed distributed gate.

For rollback, keep workers stopped. Older code does not understand EXPOSED/retry
series and can republish stale receipt verdicts. Preserve the upgraded DB and
reconcile provider evidence before resuming any older producer. Do not restore
an older DB and blindly retry pending effects. A live rollback was not tested.

## Candidate evidence and remaining gate limits

Core fix: `43e0c5b` adds receipt publication/ordering, with 3 adverse cases failing
before and passing after. The endpoint correction has a separate adverse test.
The RC handoff carries exact later candidate SHAs and validation results.
FIN-01 browser matrix is reused for unchanged UI: desktop/mobile, both themes,
identity/spoof, settings enabled/max_steps/CAS, child mandate, pending controls,
UNKNOWN/recovery, authority and dead-letter history. RC-01 did not rerun that
browser/build matrix or unrelated suites.

The pinned Guardian source probe checks the exact status route body, actual
store/model receipt handling and actual BO producer in process, without network
or colleague working-tree imports. It verifies ancestor denial, BLOCKED,
persistent fencing, correlated receipt and one effect. It is not a new HTTP gate.
The extended VAL4 upgrade probe preserves identity, provider receipt and original
outbox bytes; only the previously PENDING run submits after recovery.

A read-only Hire issue is reported for the owner: at 1286fed, an APPLIED readback
with operationId but missing key/digest remains APPLIED although identity_valid
is false (`connectors/base.py:137–162`); `executor/outbox.py:410` promotes APPLIED
to DONE without that guard. BOAgents does not consume Hire's provider readback;
this issue limits the shared candidate gate, not its local synthetic receipt
lookup. The owner/coordinator must resolve or disposition it. No Hire or Guardian
source was edited, and newer unpublished working-tree changes were not audited.
