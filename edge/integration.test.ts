import test from "node:test";
import assert from "node:assert/strict";
import http from "node:http";
import { handleTelegramWebhook } from "./handler.ts";
import type { EdgeConfig } from "./handler.ts";

test("End-to-End: Edge forwards to FastAPI stub and receives text reply", async () => {
  // 1. Start a mock FastAPI backend conforming to HTTP contract
  let receivedBearer = "";
  let receivedPayload: any = null;

  const backendServer = http.createServer((req, res) => {
    receivedBearer = req.headers["authorization"] || "";
    let data = "";
    req.on("data", (chunk) => (data += chunk));
    req.on("end", () => {
      receivedPayload = JSON.parse(data);
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(
        JSON.stringify({
          request_id: receivedPayload.request_id,
          status: "completed",
          answer: "Hello from backend stub!",
          error_code: null,
        })
      );
    });
  });

  await new Promise<void>((resolve) => backendServer.listen(0, resolve));
  const backendPort = (backendServer.address() as any).port;

  try {
    const config: EdgeConfig = {
      telegramBotToken: "", // empty so it skips calling external Telegram API
      telegramWebhookSecret: "test-webhook-secret",
      allowedUserId: 12345,
      backendUrl: `http://127.0.0.1:${backendPort}`,
      edgeBackendSecret: "test-backend-secret",
      edgePort: 8080,
      backendTimeoutMs: 5000,
      maxPromptLength: 4000,
    };

    const incomingUpdate = {
      update_id: 9999,
      message: {
        message_id: 1,
        date: 1700000000,
        chat: { id: 12345, type: "private" },
        from: { id: 12345, is_bot: false, first_name: "User" },
        text: "Hi assistant",
      },
    };

    const result = await handleTelegramWebhook("test-webhook-secret", incomingUpdate, config);

    assert.equal(result.statusCode, 200);
    assert.equal(result.responseBody.status, "completed");
    assert.equal(result.responseBody.answer, "Hello from backend stub!");
    assert.equal(receivedBearer, "Bearer test-backend-secret");
    assert.equal(receivedPayload.request_id, "tg:9999");
    assert.equal(receivedPayload.text, "Hi assistant");
  } finally {
    backendServer.close();
  }
});
