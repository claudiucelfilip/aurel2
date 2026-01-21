# Aurel2 Approval Endpoint

A Vercel serverless endpoint for managing trading decision approvals. Provides a mobile-friendly HTML interface for approving or rejecting decisions from the Aurel2 trading agent.

## Features

- **RESTful API** for creating and managing decisions
- **Mobile-friendly HTML page** with dark theme matching Aurel2 dashboard
- **Real-time status updates** with approve/reject buttons
- **Vercel KV storage** for persistent decision state

## API Endpoints

### `GET /api/decision/[id]`

Returns the decision approval page (HTML) or decision data (JSON).

**Headers:**
- `Accept: text/html` - Returns HTML approval page (default)
- `Accept: application/json` - Returns JSON decision data

**Response (JSON):**
```json
{
  "id": "decision-123",
  "action": "buy",
  "symbol": "BTCUSDT",
  "reasoning": "Strong upward trend detected...",
  "confidence": 0.85,
  "created_at": "2024-01-15T10:30:00.000Z",
  "status": "pending",
  "responded_at": null
}
```

### `POST /api/decision/[id]`

Creates a new decision.

**Request Body:**
```json
{
  "action": "buy",
  "symbol": "BTCUSDT",
  "reasoning": "Strong upward trend detected with high volume...",
  "confidence": 0.85
}
```

**Fields:**
- `action` (required): `"buy"`, `"sell"`, or `"hold"`
- `symbol` (required): Trading pair symbol (e.g., `"BTCUSDT"`)
- `reasoning` (required): Explanation for the decision
- `confidence` (required): Number between 0 and 1

### `PATCH /api/decision/[id]`

Updates a decision's status (approve/reject).

**Request Body:**
```json
{
  "status": "approved"
}
```

**Fields:**
- `status` (required): `"approved"` or `"rejected"`

## Deployment

### Prerequisites

1. [Vercel CLI](https://vercel.com/cli) installed
2. A Vercel account
3. Vercel KV database created

### Setup Vercel KV

1. Go to your Vercel dashboard
2. Navigate to **Storage** > **Create Database** > **KV**
3. Name it (e.g., `aurel2-decisions`)
4. Link it to your project

### Deploy

```bash
# Navigate to approval-endpoint directory
cd approval-endpoint

# Install dependencies
npm install

# Login to Vercel (if not already)
vercel login

# Deploy to Vercel
vercel

# For production deployment
vercel --prod
```

### Environment Variables

The following environment variables are automatically set when you link Vercel KV:

- `KV_REST_API_URL` - Vercel KV REST API URL
- `KV_REST_API_TOKEN` - Vercel KV REST API token

## Local Development

```bash
# Install dependencies
npm install

# Start development server
npm run dev
```

**Note:** For local development, you'll need to set up environment variables for Vercel KV. You can use `vercel env pull` to download them from your Vercel project.

## Usage Example

### Create a Decision (from Aurel2 agent)

```typescript
const response = await fetch('https://your-app.vercel.app/api/decision/decision-123', {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
  },
  body: JSON.stringify({
    action: 'buy',
    symbol: 'BTCUSDT',
    reasoning: 'Strong bullish divergence on RSI with increasing volume',
    confidence: 0.82,
  }),
});

const decision = await response.json();
console.log('Approval URL:', `https://your-app.vercel.app/api/decision/${decision.id}`);
```

### Check Decision Status (polling)

```typescript
const response = await fetch('https://your-app.vercel.app/api/decision/decision-123', {
  headers: {
    'Accept': 'application/json',
  },
});

const decision = await response.json();
if (decision.status === 'approved') {
  // Execute the trade
} else if (decision.status === 'rejected') {
  // Skip the trade
}
```

## Decision Lifecycle

1. **Agent creates decision** via POST with `status: "pending"`
2. **User receives notification** with link to approval page
3. **User approves/rejects** via HTML buttons or PATCH request
4. **Agent polls for status** and acts accordingly
5. **Decision expires** after 7 days
