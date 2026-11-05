import { createHmac, timingSafeEqual } from 'node:crypto';
import type { ApprovalRequest, ApprovalResponse } from './http';

function equal(a: string, b: string): boolean {
  const left = Buffer.from(a), right = Buffer.from(b);
  return left.length === right.length && timingSafeEqual(left, right);
}

export function identity(req: ApprovalRequest): 'publisher' | 'approver' | null {
  const authorization = req.headers.authorization || '';
  const publisher = process.env.AUREL2_APPROVAL_API_TOKEN || '';
  const user = process.env.AUREL2_APPROVAL_USER || '';
  const password = process.env.AUREL2_APPROVAL_PASSWORD || '';
  if (publisher.length >= 32 && authorization.startsWith('Bearer ') && equal(authorization.slice(7), publisher)) return 'publisher';
  if (user && password.length >= 32 && authorization.startsWith('Basic ')) {
    const credentials = Buffer.from(authorization.slice(6), 'base64').toString('utf8');
    if (equal(credentials, `${user}:${password}`)) return 'approver';
  }
  return null;
}

export function responseHeaders(res: ApprovalResponse): void {
  res.setHeader('Cache-Control', 'no-store');
  res.setHeader('X-Content-Type-Options', 'nosniff');
  res.setHeader('X-Frame-Options', 'DENY');
  res.setHeader('Referrer-Policy', 'no-referrer');
  res.setHeader('Content-Security-Policy', "frame-ancestors 'none'; base-uri 'none'; form-action 'self'");
}

export function csrfToken(id: string, expires: string): string {
  return createHmac('sha256', process.env.AUREL2_APPROVAL_PASSWORD || '').update(`${id}:${expires}`).digest('hex');
}

export function validCsrf(req: ApprovalRequest, id: string, expires: string): boolean {
  const token = req.headers['x-csrf-token'];
  const origin = req.headers.origin;
  const expected = process.env.AUREL2_APPROVAL_ORIGIN;
  return typeof token === 'string' && equal(token, csrfToken(id, expires))
    && req.headers['sec-fetch-site'] !== 'cross-site'
    && (!origin || (!!expected && origin === expected));
}
