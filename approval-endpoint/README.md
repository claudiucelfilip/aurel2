# Aurel2 approval endpoint

An authenticated hosted interface for reading and replying to trading approval requests. Publisher credentials can create and poll requests; human approvers use separate credentials and a request token from the page. Missing credentials deny access.

See [approval access](../docs/security/approval-access.md) for required private environment variables, deployment sequencing, and trading-policy limits.

## Routes

| Route | Authorized identity | Behavior |
|---|---|---|
| `GET /api/decision/[id]` | Publisher or approver | JSON for publishers; HTML or JSON for approvers |
| `POST /api/decision/[id]` | Publisher | Create a new request with a random identifier of at least 32 characters; existing IDs return conflict |
| `PATCH /api/decision/[id]` | Approver | Approve or reject once, within one hour, with the page's request token |
| `GET /api/test` | Publisher | Return a minimal service check |

Publishing requires `action` (`buy`, `sell`, or `hold`), `symbol`, `reasoning`, and numeric `confidence` between zero and one. Send the original ISO `created_at` timestamp so the approval window matches the producer. A reply body contains `status` (`approved` or `rejected`); the page sends its request token in `X-CSRF-Token`. Tokens and passwords never belong in URLs.

## Deployment

The platform project builds this directory with Node.js 24. Configure the private authentication variables and exact HTTPS origin before deploying. The Redis connection uses `KV_REST_API_URL` and `KV_REST_API_TOKEN` from the linked storage service.

Deployment tooling belongs to the platform or a separately managed deployment environment. The app does not bundle the Vercel CLI or its unrelated build dependencies. Coordinate the producer token and hosted endpoint changes before enabling new requests; old records without expiration fail closed.

## Validation

Install dependencies with `npm ci --ignore-scripts`. Run `npm run check` and `npm test`. The served HTTP tests use fictional decisions and a disposable local Redis over a Unix socket; `redis-server` and `redis-cli` must be on PATH. They require no production credentials and never contact a broker.

After deployment, verify denied unauthenticated access and a dedicated non-trading fixture through the served HTTPS interface before enabling its producer. A passing local test does not establish the deployment's configuration.
