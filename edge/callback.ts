import crypto from "node:crypto";
import type { EdgeConfig } from "./config.ts";
import { deliverTelegramText } from "./delivery.ts";
import type { DeliveryResult } from "./delivery.ts";

export interface CompletionEventPayload {
  event_id: string;
  job_id: string;
  request_id: string;
  user_id: number;
  chat_id: number;
  message_id?: number;
  status: "completed" | "failed";
  answer?: string | null;
  error_code?: string | null;
  created_at: number;
}

export interface CallbackVerificationResult {
  valid: boolean;
  reason?: "invalid_signature" | "expired_timestamp" | "missing_headers" | "invalid_event" | "invalid_job";
}

export interface StoredDeliveryState {
  event_id: string;
  job_id: string;
  chat_id: number;
  delivered: boolean;
  sent_message_ids: number[];
  delivered_at: number;
}

/**
 * In-memory or shared store tracking delivered completion events to ensure idempotency.
 * 
 * DELIVERY LIMITATION NOTICE:
 * Telegram sendMessage API does not support client-provided idempotency keys.
 * If Telegram sendMessage succeeds but the HTTP acknowledgment is interrupted, a retry
 * risks delivering a duplicate message. To mitigate this:
 * 1. We record delivery state and message IDs upon completion.
 * 2. Repeated completion events check this store and skip redundant Telegram sendMessage calls.
 * 3. Tool execution is strictly decoupled from delivery retries; tools are never rerun merely to resend an answer.
 */
export class DeliveryStateTracker {
  private deliveries = new Map<string, StoredDeliveryState>();
  private jobDeliveries = new Map<string, StoredDeliveryState>();

  hasEvent(eventId: string): boolean {
    return this.deliveries.has(eventId);
  }

  hasJob(jobId: string): boolean {
    return this.jobDeliveries.has(jobId);
  }

  recordDelivery(state: StoredDeliveryState): void {
    this.deliveries.set(state.event_id, state);
    this.jobDeliveries.set(state.job_id, state);
  }

  getDelivery(jobOrEventId: string): StoredDeliveryState | undefined {
    return this.deliveries.get(jobOrEventId) || this.jobDeliveries.get(jobOrEventId);
  }

  clear(): void {
    this.deliveries.clear();
    this.jobDeliveries.clear();
  }
}

export const deliveryTracker = new DeliveryStateTracker();

/**
 * Validates timestamped HMAC signature: HMAC-SHA256(secret, "${timestamp}.${rawBody}")
 */
export function verifyCallbackHMAC(
  rawBody: string | Buffer,
  signatureHeader: string | undefined,
  timestampHeader: string | undefined,
  secret: string,
  maxAgeSeconds: number = 300
): CallbackVerificationResult {
  if (!signatureHeader || !secret) {
    return { valid: false, reason: "missing_headers" };
  }

  // Extract timestamp and signature from header formats:
  // "t=1700000000,v1=abcdef..." or separate headers
  let timestampStr = timestampHeader;
  let signatureHex = signatureHeader;

  if (signatureHeader.includes("t=") && signatureHeader.includes("v1=")) {
    const parts = signatureHeader.split(",");
    for (const part of parts) {
      const [k, v] = part.split("=");
      if (k === "t") timestampStr = v;
      if (k === "v1") signatureHex = v;
    }
  }

  if (!timestampStr || !signatureHex) {
    return { valid: false, reason: "missing_headers" };
  }

  const timestamp = Number.parseInt(timestampStr, 10);
  if (Number.isNaN(timestamp)) {
    return { valid: false, reason: "missing_headers" };
  }

  // Validate expiry (default 5 minutes)
  const now = Math.floor(Date.now() / 1000);
  if (Math.abs(now - timestamp) > maxAgeSeconds) {
    return { valid: false, reason: "expired_timestamp" };
  }

  const bodyBuffer = Buffer.isBuffer(rawBody) ? rawBody : Buffer.from(rawBody, "utf-8");
  const prefixBuffer = Buffer.from(`${timestamp}.`, "utf-8");
  const messageBuffer = Buffer.concat([prefixBuffer, bodyBuffer]);

  const expectedHmac = crypto.createHmac("sha256", secret).update(messageBuffer).digest("hex");
  const expectedBuf = Buffer.from(expectedHmac, "hex");
  const actualBuf = Buffer.from(signatureHex, "hex");

  if (expectedBuf.length !== actualBuf.length || !crypto.timingSafeEqual(expectedBuf, actualBuf)) {
    return { valid: false, reason: "invalid_signature" };
  }

  return { valid: true };
}

/**
 * Handles incoming completion callback from the backend durable queue worker.
 */
export async function handleBackendCallback(
  rawBody: string,
  signatureHeader: string | undefined,
  timestampHeader: string | undefined,
  cfg: EdgeConfig
): Promise<{ statusCode: number; responseBody: Record<string, any> }> {
  // 1. Verify timestamped HMAC
  const verification = verifyCallbackHMAC(
    rawBody,
    signatureHeader,
    timestampHeader,
    cfg.edgeBackendSecret
  );

  if (!verification.valid) {
    return {
      statusCode: 401,
      responseBody: { error: "Callback authentication failed", reason: verification.reason },
    };
  }

  // 2. Parse and validate event ID and job identity
  let payload: CompletionEventPayload;
  try {
    payload = JSON.parse(rawBody);
  } catch (err: any) {
    return { statusCode: 400, responseBody: { error: "Invalid JSON payload" } };
  }

  if (!payload.event_id || typeof payload.event_id !== "string") {
    return { statusCode: 400, responseBody: { error: "Missing or invalid event_id" } };
  }

  if (!payload.job_id || typeof payload.job_id !== "string") {
    return { statusCode: 400, responseBody: { error: "Missing or invalid job_id" } };
  }

  if (!payload.chat_id || typeof payload.chat_id !== "number") {
    return { statusCode: 400, responseBody: { error: "Missing or invalid chat_id" } };
  }

  // 3. Check for repeated completion events (Deduplication / Idempotency)
  // Repeated completion events do not repeat replies
  if (deliveryTracker.hasEvent(payload.event_id) || deliveryTracker.hasJob(payload.job_id)) {
    const existing = deliveryTracker.getDelivery(payload.event_id) || deliveryTracker.getDelivery(payload.job_id);
    return {
      statusCode: 200,
      responseBody: {
        status: "already_delivered",
        event_id: payload.event_id,
        job_id: payload.job_id,
        delivered: true,
        sent_message_ids: existing?.sent_message_ids || [],
      },
    };
  }

  // 4. Deliver message to Telegram chat
  const textToDeliver = payload.answer || "Your long-running task has finished.";
  const deliveryResult: DeliveryResult = await deliverTelegramText({
    botToken: cfg.telegramBotToken,
    chatId: payload.chat_id,
    text: textToDeliver,
    deliveryBudgetMs: 15000,
  });

  // 5. Persist delivery state
  deliveryTracker.recordDelivery({
    event_id: payload.event_id,
    job_id: payload.job_id,
    chat_id: payload.chat_id,
    delivered: deliveryResult.success,
    sent_message_ids: deliveryResult.sentMessageIds,
    delivered_at: Date.now(),
  });

  return {
    statusCode: 200,
    responseBody: {
      status: "delivered",
      event_id: payload.event_id,
      job_id: payload.job_id,
      delivered: deliveryResult.success,
      sent_message_ids: deliveryResult.sentMessageIds,
      error: deliveryResult.error || null,
    },
  };
}
