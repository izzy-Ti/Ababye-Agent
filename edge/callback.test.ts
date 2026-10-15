import test from "node:test";
import assert from "node:assert/strict";
import crypto from "node:crypto";
import {
  verifyCallbackHMAC,
  handleBackendCallback,
  deliveryTracker,
  DeliveryStateTracker,
} from "./callback.ts";
import type { EdgeConfig } from "./config.ts";

const mockConfig: EdgeConfig = {
  telegramBotToken: "mock-bot-token",
  telegramWebhookSecret: "super-secret-token",
  allowedUserId: 12345,
  backendUrl: "http://127.0.0.1:8000",
  edgeBackendSecret: "test-backend-secret-123",
  edgePort: 8080,
  backendTimeoutMs: 1000,
  maxPromptLength: 4000,
};

function computeTestSignature(body: string, timestamp: number, secret: string): string {
  const message = `${timestamp}.${body}`;
  return crypto.createHmac("sha256", secret).update(message).digest("hex");
}

test("verifyCallbackHMAC accepts valid timestamped HMAC signature", () => {
  const body = JSON.stringify({ event_id: "evt_1", job_id: "job_1" });
  const timestamp = Math.floor(Date.now() / 1000);
  const signature = computeTestSignature(body, timestamp, mockConfig.edgeBackendSecret);

  // Combined header format "t=...,v1=..."
  const combinedHeader = `t=${timestamp},v1=${signature}`;
  const res1 = verifyCallbackHMAC(body, combinedHeader, undefined, mockConfig.edgeBackendSecret);
  assert.equal(res1.valid, true);

  // Separate headers format
  const res2 = verifyCallbackHMAC(body, signature, timestamp.toString(), mockConfig.edgeBackendSecret);
  assert.equal(res2.valid, true);
});

test("verifyCallbackHMAC rejects tampered body or invalid secret", () => {
  const body = JSON.stringify({ event_id: "evt_1", job_id: "job_1" });
  const timestamp = Math.floor(Date.now() / 1000);
  const signature = computeTestSignature(body, timestamp, mockConfig.edgeBackendSecret);

  // Tampered body
  const tamperedBody = JSON.stringify({ event_id: "evt_1", job_id: "job_1", extra: "tampered" });
  const res1 = verifyCallbackHMAC(tamperedBody, `t=${timestamp},v1=${signature}`, undefined, mockConfig.edgeBackendSecret);
  assert.equal(res1.valid, false);
  assert.equal(res1.reason, "invalid_signature");

  // Wrong secret
  const res2 = verifyCallbackHMAC(body, `t=${timestamp},v1=${signature}`, undefined, "wrong-secret");
  assert.equal(res2.valid, false);
  assert.equal(res2.reason, "invalid_signature");
});

test("verifyCallbackHMAC rejects expired timestamp (>300s)", () => {
  const body = JSON.stringify({ event_id: "evt_1", job_id: "job_1" });
  const oldTimestamp = Math.floor(Date.now() / 1000) - 400; // 400 seconds ago
  const signature = computeTestSignature(body, oldTimestamp, mockConfig.edgeBackendSecret);

  const res = verifyCallbackHMAC(body, `t=${oldTimestamp},v1=${signature}`, undefined, mockConfig.edgeBackendSecret, 300);
  assert.equal(res.valid, false);
  assert.equal(res.reason, "expired_timestamp");
});

test("handleBackendCallback validates event ID, job identity, and delivers answer", async () => {
  deliveryTracker.clear();

  const payload = {
    event_id: "evt_test_101",
    job_id: "job_test_101",
    request_id: "tg:101",
    user_id: 12345,
    chat_id: 12345,
    status: "completed",
    answer: "Your complex calculation is complete.",
    created_at: Date.now() / 1000,
  };
  const body = JSON.stringify(payload);
  const timestamp = Math.floor(Date.now() / 1000);
  const sig = computeTestSignature(body, timestamp, mockConfig.edgeBackendSecret);

  const result = await handleBackendCallback(
    body,
    `t=${timestamp},v1=${sig}`,
    undefined,
    mockConfig
  );

  assert.equal(result.statusCode, 200);
  assert.equal(result.responseBody.status, "delivered");
  assert.equal(result.responseBody.event_id, "evt_test_101");
  assert.equal(result.responseBody.job_id, "job_test_101");
  assert.equal(deliveryTracker.hasEvent("evt_test_101"), true);
  assert.equal(deliveryTracker.hasJob("job_test_101"), true);
});

test("repeated completion events do not repeat replies (idempotent callback delivery)", async () => {
  deliveryTracker.clear();

  const payload = {
    event_id: "evt_duplicate_check",
    job_id: "job_duplicate_check",
    request_id: "tg:102",
    user_id: 12345,
    chat_id: 12345,
    status: "completed",
    answer: "Durable job completed answer",
    created_at: Date.now() / 1000,
  };
  const body = JSON.stringify(payload);
  const timestamp = Math.floor(Date.now() / 1000);
  const sig = computeTestSignature(body, timestamp, mockConfig.edgeBackendSecret);

  // First call delivers
  const res1 = await handleBackendCallback(
    body,
    `t=${timestamp},v1=${sig}`,
    undefined,
    mockConfig
  );
  assert.equal(res1.statusCode, 200);
  assert.equal(res1.responseBody.status, "delivered");

  // Second call with same event_id / job_id skips delivery and does not repeat Telegram replies
  const res2 = await handleBackendCallback(
    body,
    `t=${timestamp},v1=${sig}`,
    undefined,
    mockConfig
  );
  assert.equal(res2.statusCode, 200);
  assert.equal(res2.responseBody.status, "already_delivered");
  assert.equal(res2.responseBody.delivered, true);
});
