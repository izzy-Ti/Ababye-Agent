import test from "node:test";
import assert from "node:assert/strict";
import http from "node:http";
import {
  verifyWebhookSecret,
  evaluateTelegramUpdate,
  normalizeTelegramUpdate,
  splitMessage,
  handleTelegramWebhook,
  callBackendAssistant,
  TypingController,
} from "./handler.ts";
import type { TelegramUpdate, EdgeConfig, NormalizedAssistantRequest } from "./handler.ts";

const mockConfig: EdgeConfig = {
  telegramBotToken: "mock-bot-token",
  telegramWebhookSecret: "super-secret-token",
  allowedUserId: 12345,
  backendUrl: "http://127.0.0.1:9999", // not called on ignored requests
  edgeBackendSecret: "mock-backend-secret",
  edgePort: 8080,
  backendTimeoutMs: 1000,
  maxPromptLength: 4000,
};

// ----------------------------------------------------
// Secret Token Verification Tests
// ----------------------------------------------------
test("verifyWebhookSecret validates correct secret with constant-time equality", () => {
  assert.equal(verifyWebhookSecret("super-secret-token", "super-secret-token"), true);
  assert.equal(verifyWebhookSecret("wrong-secret-token", "super-secret-token"), false);
  assert.equal(verifyWebhookSecret("", "super-secret-token"), false);
  assert.equal(verifyWebhookSecret(undefined, "super-secret-token"), false);
  assert.equal(verifyWebhookSecret(null, "super-secret-token"), false);
  // Length mismatch
  assert.equal(verifyWebhookSecret("super-secret", "super-secret-token"), false);
});

// ----------------------------------------------------
// Update Evaluation & Authorization Tests
// ----------------------------------------------------
test("evaluateTelegramUpdate accepts authorized private message", () => {
  const update = {
    update_id: 100,
    message: {
      message_id: 1,
      date: 1700000000,
      chat: { id: 12345, type: "private" },
      from: { id: 12345, is_bot: false, first_name: "Owner" },
      text: "Hello assistant",
    },
  };

  const result = evaluateTelegramUpdate(update, 12345);
  assert.equal(result.accepted, true);
  assert.ok(result.normalized);
  assert.equal(result.normalized.request_id, "tg:100");
  assert.equal(result.normalized.user_id, 12345);
  assert.equal(result.normalized.chat_id, 12345);
  assert.equal(result.normalized.text, "Hello assistant");
});

test("evaluateTelegramUpdate rejects unauthorized sender", () => {
  const update = {
    update_id: 101,
    message: {
      message_id: 2,
      date: 1700000000,
      chat: { id: 99999, type: "private" },
      from: { id: 99999, is_bot: false, first_name: "Attacker" },
      text: "Run unauthorized prompt",
    },
  };

  const result = evaluateTelegramUpdate(update, 12345);
  assert.equal(result.accepted, false);
  assert.equal(result.reason, "unauthorized_user");
  assert.equal(result.normalized, undefined);
});

test("evaluateTelegramUpdate rejects non-private chat types", () => {
  for (const chatType of ["group", "supergroup", "channel"]) {
    const update = {
      update_id: 102,
      message: {
        message_id: 3,
        date: 1700000000,
        chat: { id: -100123, type: chatType },
        from: { id: 12345, is_bot: false, first_name: "Owner" },
        text: "Group message",
      },
    };

    const result = evaluateTelegramUpdate(update, 12345);
    assert.equal(result.accepted, false);
    assert.equal(result.reason, "non_private_chat");
  }
});

// ----------------------------------------------------
// Ignore Policy Tests
// ----------------------------------------------------
test("evaluateTelegramUpdate ignores edited_message", () => {
  const update = {
    update_id: 103,
    edited_message: {
      message_id: 1,
      date: 1700000000,
      chat: { id: 12345, type: "private" },
      from: { id: 12345, is_bot: false, first_name: "Owner" },
      text: "Edited prompt injection",
    },
  };

  const result = evaluateTelegramUpdate(update, 12345);
  assert.equal(result.accepted, false);
  assert.equal(result.reason, "edited_message_ignored");
});

test("evaluateTelegramUpdate ignores channel_post", () => {
  const update = {
    update_id: 104,
    channel_post: {
      message_id: 5,
      date: 1700000000,
      chat: { id: -100555, type: "channel" },
      text: "Public broadcast",
    },
  };

  const result = evaluateTelegramUpdate(update, 12345);
  assert.equal(result.accepted, false);
  assert.equal(result.reason, "channel_post_ignored");
});

test("evaluateTelegramUpdate ignores unsupported update types", () => {
  const unsupportedUpdates = [
    { update_id: 105, callback_query: { id: "cq_1" } },
    { update_id: 106, inline_query: { id: "iq_1" } },
    { update_id: 107, poll: { id: "poll_1" } },
    { update_id: 108, my_chat_member: { chat: {} } },
  ];

  for (const update of unsupportedUpdates) {
    const result = evaluateTelegramUpdate(update, 12345);
    assert.equal(result.accepted, false);
    assert.equal(result.reason, "unsupported_update_type");
  }
});

// ----------------------------------------------------
// Missing Fields Safety Tests
// ----------------------------------------------------
test("evaluateTelegramUpdate safely handles missing fields and malformed structures", () => {
  const testCases = [
    null,
    undefined,
    "not an object",
    {},
    { update_id: "not_a_number" },
    { update_id: 109 }, // missing message
    { update_id: 110, message: {} }, // missing chat & from
    { update_id: 111, message: { chat: { type: "private" } } }, // missing from
    { update_id: 112, message: { chat: { type: "private" }, from: { id: 12345 } } }, // missing text
    { update_id: 113, message: { chat: { type: "private" }, from: { id: 12345 }, text: "   " } }, // whitespace text
  ];

  for (const tc of testCases) {
    assert.doesNotThrow(() => {
      const result = evaluateTelegramUpdate(tc, 12345);
      assert.equal(result.accepted, false);
    });
  }
});

test("evaluateTelegramUpdate supports captions as fallback for text", () => {
  const update = {
    update_id: 114,
    message: {
      message_id: 8,
      date: 1700000000,
      chat: { id: 12345, type: "private" },
      from: { id: 12345, is_bot: false, first_name: "Owner" },
      caption: "Analyze this document",
    },
  };

  const result = evaluateTelegramUpdate(update, 12345);
  assert.equal(result.accepted, true);
  assert.equal(result.normalized?.text, "Analyze this document");
  assert.equal(result.normalized?.update_id, 114);
  assert.equal(result.normalized?.chat_id, 12345);
  assert.equal(result.normalized?.message_id, 8);
  assert.equal(result.normalized?.user_id, 12345);
});

test("evaluateTelegramUpdate rejects prompts exceeding maxPromptLength", () => {
  const longText = "A".repeat(501);
  const update = {
    update_id: 115,
    message: {
      message_id: 9,
      date: 1700000000,
      chat: { id: 12345, type: "private" },
      from: { id: 12345, is_bot: false, first_name: "Owner" },
      text: longText,
    },
  };

  const result = evaluateTelegramUpdate(update, 12345, 500);
  assert.equal(result.accepted, false);
  assert.equal(result.reason, "prompt_too_long");
});

// ----------------------------------------------------
// TypingController Lifecycle Tests
// ----------------------------------------------------
test("TypingController starts and stops cleanly without leakage", () => {
  const controller = new TypingController("test-token", 12345);
  controller.start(500);
  assert.ok(controller);
  controller.stop();
  // Double stop should be safe
  assert.doesNotThrow(() => controller.stop());
});

// ----------------------------------------------------
// callBackendAssistant & Concise Error Replies Tests
// ----------------------------------------------------
test("callBackendAssistant returns completed answer from backend stub", async () => {
  let receivedAuth = "";
  let receivedBody: any = null;

  const server = http.createServer((req, res) => {
    receivedAuth = req.headers["authorization"] || "";
    let data = "";
    req.on("data", (chunk) => (data += chunk));
    req.on("end", () => {
      receivedBody = JSON.parse(data);
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(
        JSON.stringify({
          request_id: receivedBody.request_id,
          status: "completed",
          answer: "Hello from backend stub!",
          error_code: null,
        })
      );
    });
  });

  await new Promise<void>((resolve) => server.listen(0, resolve));
  const port = (server.address() as any).port;

  try {
    const payload: NormalizedAssistantRequest = {
      request_id: "tg:201",
      update_id: 201,
      user_id: 12345,
      chat_id: 12345,
      message_id: 10,
      text: "Hello",
    };

    const cfg: EdgeConfig = {
      ...mockConfig,
      backendUrl: `http://127.0.0.1:${port}`,
      edgeBackendSecret: "secret-token-123",
      backendTimeoutMs: 2000,
    };

    const result = await callBackendAssistant(payload, cfg);
    assert.equal(result.status, "completed");
    assert.equal(result.answer, "Hello from backend stub!");
    assert.equal(receivedAuth, "Bearer secret-token-123");
    assert.equal(receivedBody.request_id, "tg:201");
  } finally {
    server.close();
  }
});

test("callBackendAssistant handles auth failure (401) with concise reply", async () => {
  const server = http.createServer((req, res) => {
    res.writeHead(401, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ detail: "Unauthorized" }));
  });

  await new Promise<void>((resolve) => server.listen(0, resolve));
  const port = (server.address() as any).port;

  try {
    const payload: NormalizedAssistantRequest = {
      request_id: "tg:202",
      update_id: 202,
      user_id: 12345,
      chat_id: 12345,
      message_id: 11,
      text: "Hello",
    };

    const cfg: EdgeConfig = {
      ...mockConfig,
      backendUrl: `http://127.0.0.1:${port}`,
    };

    const result = await callBackendAssistant(payload, cfg);
    assert.equal(result.status, "error");
    assert.equal(result.error_code, "HTTP_401");
    assert.equal(result.user_message, "Authentication failed: Backend access is unauthorized.");
  } finally {
    server.close();
  }
});

test("callBackendAssistant handles forbidden user (403) with concise reply", async () => {
  const server = http.createServer((req, res) => {
    res.writeHead(403, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ detail: "Forbidden" }));
  });

  await new Promise<void>((resolve) => server.listen(0, resolve));
  const port = (server.address() as any).port;

  try {
    const payload: NormalizedAssistantRequest = {
      request_id: "tg:203",
      update_id: 203,
      user_id: 99999,
      chat_id: 99999,
      message_id: 12,
      text: "Forbidden prompt",
    };

    const cfg: EdgeConfig = {
      ...mockConfig,
      backendUrl: `http://127.0.0.1:${port}`,
    };

    const result = await callBackendAssistant(payload, cfg);
    assert.equal(result.status, "error");
    assert.equal(result.error_code, "HTTP_403");
    assert.equal(result.user_message, "Access denied: Your account is not authorized.");
  } finally {
    server.close();
  }
});

test("callBackendAssistant handles abort timeout with concise reply", async () => {
  // A server that hangs and never responds
  const server = http.createServer((_req, _res) => {});

  await new Promise<void>((resolve) => server.listen(0, resolve));
  const port = (server.address() as any).port;

  try {
    const payload: NormalizedAssistantRequest = {
      request_id: "tg:204",
      update_id: 204,
      user_id: 12345,
      chat_id: 12345,
      message_id: 13,
      text: "Slow prompt",
    };

    const cfg: EdgeConfig = {
      ...mockConfig,
      backendUrl: `http://127.0.0.1:${port}`,
      backendTimeoutMs: 100, // abort after 100ms
    };

    const result = await callBackendAssistant(payload, cfg);
    assert.equal(result.status, "error");
    assert.equal(result.error_code, "DEADLINE_EXCEEDED");
    assert.equal(
      result.user_message,
      "Request timed out while waiting for a response. Please try again."
    );
  } finally {
    server.close();
  }
});

test("callBackendAssistant handles unavailable backend with concise reply", async () => {
  // Point to a port where nothing is listening
  const payload: NormalizedAssistantRequest = {
    request_id: "tg:205",
    update_id: 205,
    user_id: 12345,
    chat_id: 12345,
    message_id: 14,
    text: "Offline prompt",
  };

  const cfg: EdgeConfig = {
    ...mockConfig,
    backendUrl: "http://127.0.0.1:49999",
  };

  const result = await callBackendAssistant(payload, cfg);
  assert.equal(result.status, "error");
  assert.equal(
    result.user_message,
    "Assistant service is currently unavailable. Please try again later."
  );
});

// ----------------------------------------------------
// handleTelegramWebhook Webhook Flow Tests
// ----------------------------------------------------
test("handleTelegramWebhook rejects invalid secret with 401", async () => {
  const res = await handleTelegramWebhook("wrong-token", { update_id: 1 }, mockConfig);
  assert.equal(res.statusCode, 401);
  assert.deepEqual(res.responseBody, { error: "Invalid webhook secret" });
});

test("handleTelegramWebhook ignores unauthorized update with 200 without calling backend", async () => {
  const unauthorizedUpdate = {
    update_id: 115,
    message: {
      message_id: 9,
      date: 1700000000,
      chat: { id: 99999, type: "private" },
      from: { id: 99999, is_bot: false, first_name: "Attacker" },
      text: "Should not execute",
    },
  };

  const res = await handleTelegramWebhook("super-secret-token", unauthorizedUpdate, mockConfig);
  assert.equal(res.statusCode, 200);
  assert.equal(res.responseBody.status, "ignored");
  assert.equal(res.responseBody.reason, "unauthorized_user");
});

test("handleTelegramWebhook ignores edited messages with 200 without calling backend", async () => {
  const editedUpdate = {
    update_id: 116,
    edited_message: {
      message_id: 10,
      date: 1700000000,
      chat: { id: 12345, type: "private" },
      from: { id: 12345, is_bot: false, first_name: "Owner" },
      text: "Edited message",
    },
  };

  const res = await handleTelegramWebhook("super-secret-token", editedUpdate, mockConfig);
  assert.equal(res.statusCode, 200);
  assert.equal(res.responseBody.status, "ignored");
  assert.equal(res.responseBody.reason, "edited_message_ignored");
});

test("splitMessage correctly chunks text", () => {
  const text = "A".repeat(85);
  const chunks = splitMessage(text, 50);
  assert.equal(chunks.length, 2);
  assert.equal(chunks[0].length, 50);
  assert.equal(chunks[1].length, 35);
});

test("evaluateTelegramUpdate accepts voice note and extracts file_id and metadata", () => {
  const voiceUpdate = {
    update_id: 117,
    message: {
      message_id: 25,
      date: 1700000000,
      chat: { id: 12345, type: "private" },
      from: { id: 12345, is_bot: false, first_name: "Owner" },
      voice: {
        file_id: "AwADBAADVoiceFileId123",
        duration: 8,
        mime_type: "audio/ogg",
        file_size: 15420,
      },
    },
  };

  const result = evaluateTelegramUpdate(voiceUpdate, 12345);
  assert.equal(result.accepted, true);
  assert.ok(result.normalized);
  assert.equal(result.normalized.voice?.file_id, "AwADBAADVoiceFileId123");
  assert.equal(result.normalized.voice?.mime_type, "audio/ogg");
  assert.equal(result.normalized.voice?.file_size, 15420);
  assert.equal(result.normalized.voice?.duration, 8);
});

