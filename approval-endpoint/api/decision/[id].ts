import type { VercelRequest, VercelResponse } from '@vercel/node';
import { kv } from '@vercel/kv';

// Decision interface
interface Decision {
  id: string;
  action: 'buy' | 'sell' | 'hold';
  symbol: string;
  reasoning: string;
  confidence: number;
  created_at: string;
  status: 'pending' | 'approved' | 'rejected';
  responded_at?: string;
}

// Generate the approval HTML page
function generateApprovalHTML(decision: Decision): string {
  const statusColors: Record<string, string> = {
    pending: '#f59e0b',
    approved: '#22c55e',
    rejected: '#ef4444',
  };

  const actionColors: Record<string, string> = {
    buy: '#22c55e',
    sell: '#ef4444',
    hold: '#3b82f6',
  };

  const isPending = decision.status === 'pending';
  const statusColor = statusColors[decision.status] || '#6b7280';
  const actionColor = actionColors[decision.action] || '#6b7280';

  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Aurel2 Decision: ${decision.symbol} ${decision.action.toUpperCase()}</title>
  <style>
    * {
      margin: 0;
      padding: 0;
      box-sizing: border-box;
    }

    body {
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
      background: linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%);
      min-height: 100vh;
      color: #e2e8f0;
      padding: 20px;
    }

    .container {
      max-width: 500px;
      margin: 0 auto;
    }

    .header {
      text-align: center;
      margin-bottom: 24px;
    }

    .logo {
      font-size: 28px;
      font-weight: 700;
      background: linear-gradient(90deg, #3b82f6, #8b5cf6);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      background-clip: text;
    }

    .subtitle {
      color: #94a3b8;
      font-size: 14px;
      margin-top: 4px;
    }

    .card {
      background: rgba(30, 41, 59, 0.8);
      border-radius: 16px;
      padding: 24px;
      border: 1px solid rgba(148, 163, 184, 0.1);
      box-shadow: 0 4px 24px rgba(0, 0, 0, 0.3);
    }

    .status-badge {
      display: inline-block;
      padding: 6px 16px;
      border-radius: 20px;
      font-size: 12px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      background: ${statusColor}20;
      color: ${statusColor};
      border: 1px solid ${statusColor}40;
      margin-bottom: 20px;
    }

    .action-section {
      text-align: center;
      margin-bottom: 24px;
    }

    .action-badge {
      display: inline-block;
      padding: 12px 32px;
      border-radius: 12px;
      font-size: 24px;
      font-weight: 700;
      text-transform: uppercase;
      background: ${actionColor}20;
      color: ${actionColor};
      border: 2px solid ${actionColor};
    }

    .symbol {
      font-size: 32px;
      font-weight: 700;
      margin-top: 12px;
      color: #f1f5f9;
    }

    .confidence-section {
      margin-bottom: 24px;
    }

    .label {
      font-size: 12px;
      color: #94a3b8;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      margin-bottom: 8px;
    }

    .confidence-bar {
      height: 8px;
      background: #334155;
      border-radius: 4px;
      overflow: hidden;
    }

    .confidence-fill {
      height: 100%;
      background: linear-gradient(90deg, #3b82f6, #8b5cf6);
      border-radius: 4px;
      width: ${decision.confidence * 100}%;
      transition: width 0.5s ease;
    }

    .confidence-value {
      text-align: right;
      font-size: 14px;
      color: #cbd5e1;
      margin-top: 4px;
    }

    .reasoning-section {
      margin-bottom: 24px;
    }

    .reasoning-text {
      background: rgba(15, 23, 42, 0.6);
      padding: 16px;
      border-radius: 12px;
      font-size: 14px;
      line-height: 1.6;
      color: #cbd5e1;
      border: 1px solid rgba(148, 163, 184, 0.1);
    }

    .meta {
      font-size: 12px;
      color: #64748b;
      margin-bottom: 24px;
    }

    .meta-item {
      margin-bottom: 4px;
    }

    .buttons {
      display: flex;
      gap: 12px;
    }

    .btn {
      flex: 1;
      padding: 16px 24px;
      border: none;
      border-radius: 12px;
      font-size: 16px;
      font-weight: 600;
      cursor: pointer;
      transition: all 0.2s ease;
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }

    .btn:disabled {
      opacity: 0.5;
      cursor: not-allowed;
    }

    .btn-approve {
      background: linear-gradient(135deg, #22c55e, #16a34a);
      color: white;
    }

    .btn-approve:hover:not(:disabled) {
      transform: translateY(-2px);
      box-shadow: 0 4px 12px rgba(34, 197, 94, 0.4);
    }

    .btn-reject {
      background: linear-gradient(135deg, #ef4444, #dc2626);
      color: white;
    }

    .btn-reject:hover:not(:disabled) {
      transform: translateY(-2px);
      box-shadow: 0 4px 12px rgba(239, 68, 68, 0.4);
    }

    .response-message {
      text-align: center;
      padding: 20px;
      background: rgba(15, 23, 42, 0.6);
      border-radius: 12px;
      border: 1px solid rgba(148, 163, 184, 0.1);
    }

    .response-icon {
      font-size: 48px;
      margin-bottom: 12px;
    }

    .response-text {
      font-size: 18px;
      font-weight: 600;
      color: ${statusColor};
    }

    .loading {
      display: none;
      text-align: center;
      padding: 20px;
    }

    .spinner {
      width: 40px;
      height: 40px;
      border: 3px solid #334155;
      border-top-color: #3b82f6;
      border-radius: 50%;
      animation: spin 1s linear infinite;
      margin: 0 auto 12px;
    }

    @keyframes spin {
      to { transform: rotate(360deg); }
    }

    .error-message {
      display: none;
      background: rgba(239, 68, 68, 0.1);
      border: 1px solid rgba(239, 68, 68, 0.3);
      color: #ef4444;
      padding: 12px;
      border-radius: 8px;
      margin-top: 12px;
      text-align: center;
    }
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <div class="logo">AUREL2</div>
      <div class="subtitle">Trading Decision Approval</div>
    </div>

    <div class="card">
      <div class="status-badge">${decision.status}</div>

      <div class="action-section">
        <div class="action-badge">${decision.action}</div>
        <div class="symbol">${decision.symbol}</div>
      </div>

      <div class="confidence-section">
        <div class="label">Confidence</div>
        <div class="confidence-bar">
          <div class="confidence-fill"></div>
        </div>
        <div class="confidence-value">${(decision.confidence * 100).toFixed(1)}%</div>
      </div>

      <div class="reasoning-section">
        <div class="label">Reasoning</div>
        <div class="reasoning-text">${escapeHtml(decision.reasoning)}</div>
      </div>

      <div class="meta">
        <div class="meta-item">ID: ${decision.id}</div>
        <div class="meta-item">Created: ${new Date(decision.created_at).toLocaleString()}</div>
        ${decision.responded_at ? `<div class="meta-item">Responded: ${new Date(decision.responded_at).toLocaleString()}</div>` : ''}
      </div>

      ${isPending ? `
      <div class="buttons" id="buttons">
        <button class="btn btn-reject" onclick="respond('rejected')">Reject</button>
        <button class="btn btn-approve" onclick="respond('approved')">Approve</button>
      </div>
      <div class="loading" id="loading">
        <div class="spinner"></div>
        <div>Processing...</div>
      </div>
      <div class="error-message" id="error"></div>
      ` : `
      <div class="response-message">
        <div class="response-icon">${decision.status === 'approved' ? '&#x2713;' : '&#x2717;'}</div>
        <div class="response-text">${decision.status === 'approved' ? 'Decision Approved' : 'Decision Rejected'}</div>
      </div>
      `}
    </div>
  </div>

  ${isPending ? `
  <script>
    async function respond(status) {
      const buttons = document.getElementById('buttons');
      const loading = document.getElementById('loading');
      const error = document.getElementById('error');

      buttons.style.display = 'none';
      loading.style.display = 'block';
      error.style.display = 'none';

      try {
        const response = await fetch(window.location.pathname, {
          method: 'PATCH',
          headers: {
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({ status }),
        });

        if (!response.ok) {
          throw new Error('Failed to update decision');
        }

        // Reload to show updated status
        window.location.reload();
      } catch (err) {
        loading.style.display = 'none';
        buttons.style.display = 'flex';
        error.textContent = err.message || 'An error occurred';
        error.style.display = 'block';
      }
    }
  </script>
  ` : ''}
</body>
</html>`;
}

// Escape HTML to prevent XSS
function escapeHtml(text: string): string {
  const htmlEntities: Record<string, string> = {
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  };
  return text.replace(/[&<>"']/g, (char) => htmlEntities[char] || char);
}

// Generate decision key for KV storage
function getDecisionKey(id: string): string {
  return `decision:${id}`;
}

export default async function handler(
  req: VercelRequest,
  res: VercelResponse
) {
  // Get decision ID from URL
  const { id } = req.query;

  if (!id || typeof id !== 'string') {
    return res.status(400).json({ error: 'Decision ID is required' });
  }

  const decisionKey = getDecisionKey(id);

  try {
    switch (req.method) {
      // GET - Returns approval HTML page or JSON
      case 'GET': {
        const decision = await kv.get<Decision>(decisionKey);

        if (!decision) {
          return res.status(404).json({ error: 'Decision not found' });
        }

        // Check Accept header for JSON preference
        const acceptHeader = req.headers.accept || '';
        if (acceptHeader.includes('application/json')) {
          return res.status(200).json(decision);
        }

        // Return HTML page
        res.setHeader('Content-Type', 'text/html');
        return res.status(200).send(generateApprovalHTML(decision));
      }

      // POST - Creates new decision
      case 'POST': {
        const { action, symbol, reasoning, confidence } = req.body;

        // Validate required fields
        if (!action || !symbol || !reasoning || confidence === undefined) {
          return res.status(400).json({
            error: 'Missing required fields: action, symbol, reasoning, confidence',
          });
        }

        // Validate action
        if (!['buy', 'sell', 'hold'].includes(action)) {
          return res.status(400).json({
            error: 'Invalid action. Must be buy, sell, or hold',
          });
        }

        // Validate confidence
        if (typeof confidence !== 'number' || confidence < 0 || confidence > 1) {
          return res.status(400).json({
            error: 'Confidence must be a number between 0 and 1',
          });
        }

        // Check if decision already exists
        const existing = await kv.get<Decision>(decisionKey);
        if (existing) {
          return res.status(409).json({
            error: 'Decision with this ID already exists',
          });
        }

        // Create new decision
        const decision: Decision = {
          id,
          action,
          symbol,
          reasoning,
          confidence,
          created_at: new Date().toISOString(),
          status: 'pending',
        };

        // Store in KV with 7-day expiration
        await kv.set(decisionKey, decision, { ex: 7 * 24 * 60 * 60 });

        return res.status(201).json(decision);
      }

      // PATCH - Updates decision status
      case 'PATCH': {
        const { status } = req.body;

        // Validate status
        if (!status || !['approved', 'rejected'].includes(status)) {
          return res.status(400).json({
            error: 'Invalid status. Must be approved or rejected',
          });
        }

        // Get existing decision
        const decision = await kv.get<Decision>(decisionKey);
        if (!decision) {
          return res.status(404).json({ error: 'Decision not found' });
        }

        // Check if already responded
        if (decision.status !== 'pending') {
          return res.status(409).json({
            error: 'Decision has already been responded to',
          });
        }

        // Update decision
        const updatedDecision: Decision = {
          ...decision,
          status,
          responded_at: new Date().toISOString(),
        };

        // Store updated decision
        await kv.set(decisionKey, updatedDecision, { ex: 7 * 24 * 60 * 60 });

        return res.status(200).json(updatedDecision);
      }

      default:
        res.setHeader('Allow', ['GET', 'POST', 'PATCH']);
        return res.status(405).json({ error: `Method ${req.method} not allowed` });
    }
  } catch (error) {
    console.error('Error handling decision:', error);
    return res.status(500).json({ error: 'Internal server error' });
  }
}
