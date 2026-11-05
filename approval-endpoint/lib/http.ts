import type { IncomingMessage, ServerResponse } from 'node:http';

// Structural types for the platform's Node HTTP helpers; no deployment CLI dependency.
export interface ApprovalRequest extends IncomingMessage {
  query: Record<string, string | string[] | undefined>;
  body?: Record<string, any>;
}

export interface ApprovalResponse extends ServerResponse {
  status(code: number): ApprovalResponse;
  json(body: unknown): ApprovalResponse;
  send(body: string): ApprovalResponse;
}
