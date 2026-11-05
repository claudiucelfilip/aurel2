import type { ApprovalRequest, ApprovalResponse } from '../lib/http';
import { identity, responseHeaders } from '../lib/security';

export default function handler(req: ApprovalRequest, res: ApprovalResponse) {
  responseHeaders(res);
  if (identity(req) !== 'publisher') return res.status(401).json({ error: 'Authentication required' });
  if (req.method !== 'GET') return res.status(405).json({ error: 'Method not allowed' });
  return res.status(200).json({ ok: true });
}
