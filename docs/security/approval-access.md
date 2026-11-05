# Approval access

The dashboard and hosted approval endpoint deny requests until credentials are configured. Publisher credentials can create and poll decisions; they cannot approve them. Human approvers authenticate separately. Approval links contain random identifiers, never credentials.

## Configuration

Use independently generated secrets of at least 32 characters. Keep them outside Git and inject them through the deployment's private environment store.

| Component | Variables | Access |
|---|---|---|
| Dashboard approver | `AUREL2_DASHBOARD_USER`, `AUREL2_DASHBOARD_PASSWORD` | Read dashboard and approve/reject pending decisions |
| Optional dashboard reader | `AUREL2_DASHBOARD_READ_USER`, `AUREL2_DASHBOARD_READ_PASSWORD` | Read only |
| Dashboard origin | `AUREL2_DASHBOARD_ORIGIN` | Exact public HTTPS origin used to check browser requests |
| Publishing/polling daemon or CLI | `AUREL2_APPROVAL_API_TOKEN` | Create/poll only; same token configured at its destination |
| Hosted endpoint approver | `AUREL2_APPROVAL_USER`, `AUREL2_APPROVAL_PASSWORD` | Read and approve/reject; cannot publish decisions |
| Hosted endpoint origin | `AUREL2_APPROVAL_ORIGIN` | Exact public HTTPS origin for browser request checks |

Use distinct publisher tokens for separate endpoint deployments. Configure `AUREL2_APPROVAL_BASE_URL` in the daemon's private environment to its intended endpoint. Never place passwords or tokens in the URL. Remote publisher URLs require HTTPS; loopback HTTP is allowed for local communication and fixtures. Cloudflare must redirect HTTP before showing a Basic-auth challenge. Keep personal interfaces behind an identity-based edge gate as well.

The dashboard Docker port now binds only to loopback. The existing optional dashboard profile is still optional; do not start a retired dashboard merely to apply this patch. The active live-trading compose file may be an operational file outside this repository: adapt its environment separately, without replacing it with the paper compose template.

## Approval lifecycle

Both Python decision creators use 192-bit random identifiers. Hosted creation is publisher-only and uses Redis `SET NX`; replay cannot overwrite an existing record. Hosted approval expires one hour after creation, including legacy records lacking an expiration. Authenticated browser approval requires a decision-specific request token. Redis checks expiry using server time and applies exactly one reply atomically. Answered records remain available to authenticated polling for seven days.

Local approval uses the existing creation time and one-hour approval window. A shared filesystem lock serializes dashboard replies; only a pending, unexpired decision can change. Repeated or competing replies return conflict. Atomic owner-only files avoid partial writes, and a stale pending daemon save preserves a completed dashboard response. A remote poll cannot overwrite a dashboard reply that arrived in flight. Remote poll replies are emitted once during a daemon session.

These controls do not change trading strategies, broker execution, or the existing timeout auto-execution policy. They do not provide exactly-once broker execution across a daemon crash. The pending-file editor and daemon remain trusted local components: isolate their operating-system identities and credentials; middleware cannot stop an already-compromised process with write access to that file.

## Validation

Python security tests enumerate every registered dashboard route without credentials, verify publisher/reader restrictions, cross-origin and missing-token rejection, expired and duplicate replies, concurrent approval/rejection, HTML escaping, and a real loopback HTTP approval flow with fictional state. No broker or trading process is started.

The hosted endpoint tests run a loopback HTTP server and disposable local Redis with no network listener, using fictional decisions. They verify denied unauthenticated methods, role separation, CSRF, expiry, replay, and concurrent replies using the production Lua transaction. They require `redis-server` and `redis-cli` on PATH. Run `npm test` and `npm run check` from `approval-endpoint`.

The hosted configuration also disables public source/build-log views for new deployments. Existing deployments need separate review and retirement if exposed; this setting is not retroactive.

Deploy the hosted endpoint only after configuring fresh credentials and its origin. Revalidate the actual public routes with no credentials and with a dedicated non-trading fixture before enabling any producer. Never use a real pending trade for validation.
