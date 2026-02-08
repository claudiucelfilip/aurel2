import type { VercelRequest, VercelResponse } from '@vercel/node';
import { Redis } from '@upstash/redis';

// Initialize Redis - try fromEnv() first, then fall back to explicit config
const redis = new Redis({
  url: process.env.UPSTASH_REDIS_REST_URL || process.env.KV_REST_API_URL || '',
  token: process.env.UPSTASH_REDIS_REST_TOKEN || process.env.KV_REST_API_TOKEN || '',
});

// Strategy signal interface
interface StrategySignal {
  name: string;
  action: string;
  confidence: number;
}

// Decision interface with rich context
interface Decision {
  id: string;
  action: 'buy' | 'sell' | 'hold';
  symbol: string;
  reasoning: string;
  confidence: number;
  created_at: string;
  status: 'pending' | 'approved' | 'rejected';
  responded_at?: string;
  // Rich context
  deterministic_action?: string;
  deterministic_asset?: string;
  ai_agrees?: boolean;
  ai_action?: string;
  ai_asset?: string;
  ai_reasoning?: string;
  strategies_agree?: boolean;
  strategies?: StrategySignal[];
  market_regime?: string;
  spy_price?: number;
  drawdown?: number;
  current_holding?: string;
  ai_commentary?: string;
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
    BUY: '#22c55e',
    SELL: '#ef4444',
    HOLD: '#3b82f6',
  };

  const isPending = decision.status === 'pending';
  const statusColor = statusColors[decision.status] || '#6b7280';
  const actionColor = actionColors[decision.action] || '#6b7280';

  // Generate strategies HTML
  const strategiesHtml = decision.strategies?.map(s => {
    const color = actionColors[s.action] || '#6b7280';
    return `
      <div class="strategy-row">
        <span class="strategy-name">${s.name}</span>
        <span class="strategy-action" style="color: ${color}">${s.action}</span>
      </div>
    `;
  }).join('') || '';

  // Determine agreement status
  const hasRichContext = decision.ai_agrees !== undefined;
  const aiAgrees = decision.ai_agrees ?? true;
  const strategiesAgree = decision.strategies_agree ?? true;
  const allAgree = aiAgrees && strategiesAgree;

  // Agreement banner
  let agreementBanner = '';
  if (hasRichContext) {
    if (allAgree) {
      agreementBanner = `
        <div class="agreement-banner agree">
          <div class="agreement-icon">✓</div>
          <div class="agreement-text">
            <strong>Full Agreement</strong>
            <span>All 3 strategies and the AI recommend the same action</span>
          </div>
        </div>
      `;
    } else if (!strategiesAgree && aiAgrees) {
      agreementBanner = `
        <div class="agreement-banner partial">
          <div class="agreement-icon">⚖️</div>
          <div class="agreement-text">
            <strong>Mixed Strategy Signals</strong>
            <span>The 3 strategies below don't fully agree, but the AI confirms the final recommendation is sound</span>
          </div>
        </div>
      `;
    } else if (!aiAgrees) {
      agreementBanner = `
        <div class="agreement-banner disagree">
          <div class="agreement-icon">🤖</div>
          <div class="agreement-text">
            <strong>AI Override</strong>
            <span>The AI suggests a different action than the base system</span>
          </div>
        </div>
      `;
    }
  }

  // Market context section
  const marketHtml = decision.market_regime ? `
    <div class="market-context">
      <div class="label">Market Conditions</div>
      <div class="market-grid">
        <div class="market-item">
          <span class="market-label">Regime</span>
          <span class="market-value">${decision.market_regime}</span>
        </div>
        ${decision.spy_price ? `
        <div class="market-item">
          <span class="market-label">S&P 500</span>
          <span class="market-value">$${decision.spy_price.toFixed(2)}</span>
        </div>
        ` : ''}
        ${decision.drawdown !== undefined ? `
        <div class="market-item">
          <span class="market-label">Drawdown</span>
          <span class="market-value ${decision.drawdown > 0.1 ? 'warning' : ''}">${(decision.drawdown * 100).toFixed(1)}%</span>
        </div>
        ` : ''}
        ${decision.current_holding ? `
        <div class="market-item">
          <span class="market-label">Current</span>
          <span class="market-value">${decision.current_holding}</span>
        </div>
        ` : ''}
      </div>
    </div>
  ` : '';

  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Aurel2: ${decision.action.toUpperCase()} ${decision.symbol}</title>
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
      padding: 16px;
    }

    .container {
      max-width: 420px;
      margin: 0 auto;
    }

    .header {
      text-align: center;
      margin-bottom: 20px;
    }

    .logo {
      font-size: 24px;
      font-weight: 700;
      background: linear-gradient(90deg, #3b82f6, #8b5cf6);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      background-clip: text;
    }

    .subtitle {
      color: #94a3b8;
      font-size: 13px;
      margin-top: 4px;
    }

    .card {
      background: rgba(30, 41, 59, 0.9);
      border-radius: 16px;
      padding: 20px;
      border: 1px solid rgba(148, 163, 184, 0.1);
      box-shadow: 0 4px 24px rgba(0, 0, 0, 0.3);
      margin-bottom: 16px;
    }

    .agreement-banner {
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 14px;
      border-radius: 12px;
      margin-bottom: 20px;
    }

    .agreement-banner.agree {
      background: rgba(34, 197, 94, 0.15);
      border: 1px solid rgba(34, 197, 94, 0.3);
    }

    .agreement-banner.partial {
      background: rgba(245, 158, 11, 0.15);
      border: 1px solid rgba(245, 158, 11, 0.3);
    }

    .agreement-banner.disagree {
      background: rgba(239, 68, 68, 0.15);
      border: 1px solid rgba(239, 68, 68, 0.3);
    }

    .agreement-icon {
      font-size: 24px;
    }

    .agreement-text {
      display: flex;
      flex-direction: column;
      gap: 2px;
    }

    .agreement-text strong {
      font-size: 14px;
      color: #f1f5f9;
    }

    .agreement-text span {
      font-size: 12px;
      color: #94a3b8;
    }

    .status-badge {
      display: inline-block;
      padding: 5px 12px;
      border-radius: 20px;
      font-size: 11px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      background: ${statusColor}20;
      color: ${statusColor};
      border: 1px solid ${statusColor}40;
      margin-bottom: 16px;
    }

    .action-section {
      text-align: center;
      margin-bottom: 20px;
    }

    .action-badge {
      display: inline-block;
      padding: 10px 28px;
      border-radius: 10px;
      font-size: 22px;
      font-weight: 700;
      text-transform: uppercase;
      background: ${actionColor}20;
      color: ${actionColor};
      border: 2px solid ${actionColor};
    }

    .symbol {
      font-size: 28px;
      font-weight: 700;
      margin-top: 10px;
      color: #f1f5f9;
    }

    .label {
      font-size: 11px;
      color: #94a3b8;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      margin-bottom: 8px;
      font-weight: 600;
    }

    .section {
      margin-bottom: 20px;
    }

    .strategies-list {
      background: rgba(15, 23, 42, 0.5);
      border-radius: 10px;
      padding: 12px;
    }

    .strategy-row {
      display: flex;
      justify-content: space-between;
      padding: 8px 0;
      border-bottom: 1px solid rgba(148, 163, 184, 0.1);
    }

    .strategy-row:last-child {
      border-bottom: none;
    }

    .strategy-name {
      color: #cbd5e1;
      font-size: 13px;
    }

    .strategy-action {
      font-weight: 600;
      font-size: 13px;
    }

    .reasoning-text {
      background: rgba(15, 23, 42, 0.5);
      padding: 14px;
      border-radius: 10px;
      font-size: 13px;
      line-height: 1.5;
      color: #cbd5e1;
    }

    .market-context {
      margin-bottom: 20px;
    }

    .market-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
    }

    .market-item {
      background: rgba(15, 23, 42, 0.5);
      padding: 10px;
      border-radius: 8px;
      display: flex;
      flex-direction: column;
      gap: 2px;
    }

    .market-label {
      font-size: 10px;
      color: #64748b;
      text-transform: uppercase;
    }

    .market-value {
      font-size: 14px;
      font-weight: 600;
      color: #f1f5f9;
    }

    .market-value.warning {
      color: #f59e0b;
    }

    .comparison-section {
      margin-bottom: 20px;
    }

    .comparison-title {
      font-size: 11px;
      color: #94a3b8;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      margin-bottom: 12px;
      font-weight: 600;
      text-align: center;
    }

    .comparison-grid {
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 12px;
    }

    .comparison-box {
      flex: 1;
      max-width: 140px;
      background: rgba(15, 23, 42, 0.5);
      padding: 14px;
      border-radius: 12px;
      text-align: center;
      display: flex;
      flex-direction: column;
      gap: 4px;
    }

    .comparison-box.original {
      border: 1px solid rgba(100, 116, 139, 0.3);
    }

    .comparison-box.override {
      border: 2px solid rgba(139, 92, 246, 0.5);
      background: rgba(139, 92, 246, 0.1);
    }

    .comparison-box.agree {
      border: 2px solid rgba(34, 197, 94, 0.5);
      background: rgba(34, 197, 94, 0.1);
    }

    .comparison-label {
      font-size: 10px;
      color: #64748b;
      text-transform: uppercase;
    }

    .comparison-action {
      font-size: 20px;
      font-weight: 700;
    }

    .comparison-asset {
      font-size: 14px;
      color: #cbd5e1;
      font-weight: 500;
    }

    .comparison-arrow {
      font-size: 24px;
      color: #64748b;
    }

    .confidence-bar {
      height: 6px;
      background: #334155;
      border-radius: 3px;
      overflow: hidden;
    }

    .confidence-fill {
      height: 100%;
      background: linear-gradient(90deg, #3b82f6, #8b5cf6);
      border-radius: 3px;
      width: ${decision.confidence * 100}%;
    }

    .confidence-value {
      text-align: right;
      font-size: 13px;
      color: #cbd5e1;
      margin-top: 4px;
    }

    .meta {
      font-size: 11px;
      color: #64748b;
      margin-bottom: 20px;
    }

    .buttons {
      display: flex;
      gap: 12px;
    }

    .btn {
      flex: 1;
      padding: 14px 20px;
      border: none;
      border-radius: 10px;
      font-size: 15px;
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
      <div class="subtitle">Trading Decision</div>
    </div>

    <div class="card">
      <div class="status-badge">${decision.status}</div>

      ${agreementBanner}

      ${decision.deterministic_action ? `
      <div class="comparison-section">
        <div class="comparison-title">${decision.ai_agrees === false ? 'What Changed' : 'Decision Summary'}</div>
        <div class="comparison-grid">
          <div class="comparison-box original">
            <span class="comparison-label">Rules Say</span>
            <span class="comparison-action" style="color: ${actionColors[decision.deterministic_action?.toUpperCase()] || '#6b7280'}">${decision.deterministic_action?.toUpperCase()}</span>
            <span class="comparison-asset">${decision.deterministic_asset || '-'}</span>
          </div>
          <div class="comparison-arrow">${decision.ai_agrees === false ? '→' : '='}</div>
          <div class="comparison-box ${decision.ai_agrees === false ? 'override' : 'agree'}">
            <span class="comparison-label">AI Says</span>
            <span class="comparison-action" style="color: ${actionColors[decision.ai_action?.toUpperCase()] || '#6b7280'}">${decision.ai_action?.toUpperCase()}</span>
            <span class="comparison-asset">${decision.ai_asset || decision.deterministic_asset || '-'}</span>
          </div>
        </div>
      </div>
      ` : `
      <div class="action-section">
        <div class="action-badge">${decision.action}</div>
        <div class="symbol">${decision.symbol}</div>
      </div>
      `}

      ${marketHtml}

      ${decision.strategies && decision.strategies.length > 0 ? `
      <div class="section">
        <div class="label">Strategy Signals</div>
        <div class="strategies-list">
          ${strategiesHtml}
        </div>
      </div>
      ` : ''}

      <div class="section">
        <div class="label">Confidence</div>
        <div class="confidence-bar">
          <div class="confidence-fill"></div>
        </div>
        <div class="confidence-value">${(decision.confidence * 100).toFixed(0)}%</div>
      </div>

      <div class="section">
        <div class="label">Why This Recommendation</div>
        <div class="reasoning-text">${escapeHtml(decision.ai_reasoning || decision.reasoning)}</div>
      </div>

      ${decision.ai_commentary ? `
      <div class="section">
        <div class="label">AI Risk Assessment</div>
        <div class="reasoning-text" style="border-left: 3px solid #3b82f6; background: rgba(59, 130, 246, 0.08);">
          ${escapeHtml(decision.ai_commentary)}
        </div>
      </div>
      ` : ''}

      ${decision.current_holding && decision.current_holding !== decision.symbol ? `
      <div class="section">
        <div class="label">Portfolio Change</div>
        <div class="reasoning-text" style="text-align: center;">
          <span style="color: #94a3b8;">${decision.current_holding}</span>
          <span style="margin: 0 12px;">→</span>
          <span style="color: ${actionColor}; font-weight: 600;">${decision.symbol}</span>
        </div>
      </div>
      ` : ''}

      <div class="meta">
        <div class="meta-item">ID: ${decision.id} • ${new Date(decision.created_at).toLocaleString()}</div>
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
        <div class="response-icon">${decision.status === 'approved' ? '✓' : '✗'}</div>
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

  // Debug: Check env vars
  if (!process.env.KV_REST_API_URL || !process.env.KV_REST_API_TOKEN) {
    return res.status(500).json({ 
      error: 'Redis not configured',
      hasUrl: !!process.env.KV_REST_API_URL,
      hasToken: !!process.env.KV_REST_API_TOKEN,
    });
  }

  const decisionKey = getDecisionKey(id);

  try {
    switch (req.method) {
      // GET - Returns approval HTML page or JSON
      case 'GET': {
        const decision = await redis.get<Decision>(decisionKey);

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
        const { 
          action, symbol, reasoning, confidence,
          // Rich context fields
          deterministic_action, deterministic_asset,
          ai_agrees, ai_action, ai_asset, ai_reasoning,
          strategies_agree, strategies,
          market_regime, spy_price, drawdown, current_holding,
          ai_commentary
        } = req.body;

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
        const existing = await redis.get<Decision>(decisionKey);
        if (existing) {
          return res.status(409).json({
            error: 'Decision with this ID already exists',
          });
        }

        // Create new decision with rich context
        const decision: Decision = {
          id,
          action,
          symbol,
          reasoning,
          confidence,
          created_at: new Date().toISOString(),
          status: 'pending',
          // Rich context (optional fields)
          deterministic_action,
          deterministic_asset,
          ai_agrees,
          ai_action,
          ai_asset,
          ai_reasoning,
          strategies_agree,
          strategies,
          market_regime,
          spy_price,
          drawdown,
          current_holding,
          ai_commentary,
        };

        // Store in KV with 7-day expiration
        await redis.set(decisionKey, decision, { ex: 7 * 24 * 60 * 60 });

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
        const decision = await redis.get<Decision>(decisionKey);
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
        await redis.set(decisionKey, updatedDecision, { ex: 7 * 24 * 60 * 60 });

        return res.status(200).json(updatedDecision);
      }

      default:
        res.setHeader('Allow', ['GET', 'POST', 'PATCH']);
        return res.status(405).json({ error: `Method ${req.method} not allowed` });
    }
  } catch (error) {
    console.error('Error handling decision:', error);
    const message = error instanceof Error ? error.message : 'Unknown error';
    return res.status(500).json({ error: 'Internal server error', details: message });
  }
}
