import test from "node:test";
import assert from "node:assert/strict";
import {
  setTelegramWebhook,
  getTelegramWebhookInfo,
  deleteTelegramWebhook,
} from "./register_webhook.ts";

test("setTelegramWebhook validates HTTPS requirement", async () => {
  await assert.rejects(
    async () => {
      await setTelegramWebhook("fake_token", {
        url: "http://insecure.example.com/webhook",
      });
    },
    { message: /must be a valid HTTPS endpoint/ }
  );
});

test("setTelegramWebhook requires bot token", async () => {
  await assert.rejects(
    async () => {
      await setTelegramWebhook("", {
        url: "https://secure.example.com/webhook",
      });
    },
    { message: /TELEGRAM_BOT_TOKEN is required/ }
  );
});

test("setTelegramWebhook dispatches correct payload to Telegram API", async () => {
  const originalFetch = globalThis.fetch;
  let interceptedUrl = "";
  let interceptedBody: any = null;

  globalThis.fetch = (async (url: any, init?: any) => {
    interceptedUrl = url.toString();
    interceptedBody = JSON.parse(init?.body || "{}");
    return new Response(JSON.stringify({ ok: true, result: true, description: "Webhook was set" }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }) as any;

  try {
    const res = await setTelegramWebhook("12345:TOKEN", {
      url: "https://assistant.example.com/webhook",
      secretToken: "webhook_secret_123",
      dropPendingUpdates: true,
      allowedUpdates: ["message"],
    });

    assert.equal(res.ok, true);
    assert.match(interceptedUrl, /api\.telegram\.org\/bot12345:TOKEN\/setWebhook/);
    assert.equal(interceptedBody.url, "https://assistant.example.com/webhook");
    assert.equal(interceptedBody.secret_token, "webhook_secret_123");
    assert.equal(interceptedBody.drop_pending_updates, true);
    assert.deepEqual(interceptedBody.allowed_updates, ["message"]);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("getTelegramWebhookInfo queries Telegram API", async () => {
  const originalFetch = globalThis.fetch;
  let interceptedUrl = "";

  globalThis.fetch = (async (url: any) => {
    interceptedUrl = url.toString();
    return new Response(
      JSON.stringify({
        ok: true,
        result: {
          url: "https://assistant.example.com/webhook",
          has_custom_certificate: false,
          pending_update_count: 0,
        },
      }),
      { status: 200, headers: { "Content-Type": "application/json" } }
    );
  }) as any;

  try {
    const res = await getTelegramWebhookInfo("12345:TOKEN");
    assert.equal(res.ok, true);
    assert.equal(res.result.url, "https://assistant.example.com/webhook");
    assert.match(interceptedUrl, /api\.telegram\.org\/bot12345:TOKEN\/getWebhookInfo/);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("deleteTelegramWebhook calls deleteWebhook endpoint", async () => {
  const originalFetch = globalThis.fetch;
  let interceptedUrl = "";
  let interceptedBody: any = null;

  globalThis.fetch = (async (url: any, init?: any) => {
    interceptedUrl = url.toString();
    interceptedBody = JSON.parse(init?.body || "{}");
    return new Response(JSON.stringify({ ok: true, result: true }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }) as any;

  try {
    const res = await deleteTelegramWebhook("12345:TOKEN", true);
    assert.equal(res.ok, true);
    assert.match(interceptedUrl, /api\.telegram\.org\/bot12345:TOKEN\/deleteWebhook/);
    assert.equal(interceptedBody.drop_pending_updates, true);
  } finally {
    globalThis.fetch = originalFetch;
  }
});
