import test from "node:test";
import assert from "node:assert/strict";
import {
  verifyWebhookSecret,
  normalizeTelegramUpdate,
  splitMessage,
} from "./handler.ts";
import type { TelegramUpdate } from "./handler.ts";

test("verifyWebhookSecret validates correct secret", () => {
  assert.equal(verifyWebhookSecret("my-secret", "my-secret"), true);
  assert.equal(verifyWebhookSecret("wrong-secret", "my-secret"), false);
  assert.equal(verifyWebhookSecret(undefined, "my-secret"), false);
});

test("normalizeTelegramUpdate accepts authorized private message", () => {
  const update: TelegramUpdate = {
    update_id: 12345,
    message: {
      message_id: 10,
      date: 1700000000,
      chat: { id: 999, type: "private" },
      from: { id: 999, is_bot: false, first_name: "Owner" },
      text: "Hello assistant",
    },
  };

  const normalized = normalizeTelegramUpdate(update, 999);
  assert.ok(normalized);
  assert.equal(normalized.request_id, "tg:12345");
  assert.equal(normalized.update_id, 12345);
  assert.equal(normalized.user_id, 999);
  assert.equal(normalized.chat_id, 999);
  assert.equal(normalized.text, "Hello assistant");
});

test("normalizeTelegramUpdate rejects unauthorized sender", () => {
  const update: TelegramUpdate = {
    update_id: 12346,
    message: {
      message_id: 11,
      date: 1700000000,
      chat: { id: 888, type: "private" },
      from: { id: 888, is_bot: false, first_name: "Stranger" },
      text: "Hello assistant",
    },
  };

  const normalized = normalizeTelegramUpdate(update, 999);
  assert.equal(normalized, null);
});

test("normalizeTelegramUpdate rejects non-private chat", () => {
  const update: TelegramUpdate = {
    update_id: 12347,
    message: {
      message_id: 12,
      date: 1700000000,
      chat: { id: -100123456, type: "group" },
      from: { id: 999, is_bot: false, first_name: "Owner" },
      text: "Hello assistant",
    },
  };

  const normalized = normalizeTelegramUpdate(update, 999);
  assert.equal(normalized, null);
});

test("splitMessage splits long text correctly", () => {
  const shortText = "Hello world";
  assert.deepEqual(splitMessage(shortText, 50), ["Hello world"]);

  const longText = "A".repeat(80);
  const chunks = splitMessage(longText, 50);
  assert.equal(chunks.length, 2);
  assert.equal(chunks[0].length, 50);
  assert.equal(chunks[1].length, 30);
});
