import test from "node:test";
import assert from "node:assert/strict";
import { splitMessageUnicode, deliverTelegramText } from "./delivery.ts";

test("splitMessageUnicode preserves Unicode surrogate pairs and grapheme clusters", () => {
  // Complex emoji sequences
  const familyEmoji = "👨‍👩‍👧‍👦"; // 7 code units
  const rainbowFlag = "🏳️‍🌈";
  const amharicText = "ሰላም አለም! "; // Ge'ez / Amharic script

  // Construct text with emojis right around chunk boundary
  const filler = "A".repeat(45);
  const textWithEmoji = filler + familyEmoji + rainbowFlag + filler;

  const chunks = splitMessageUnicode(textWithEmoji, 50);
  assert.ok(chunks.length >= 2);

  // Validate that no chunk ends with a lone surrogate
  for (const chunk of chunks) {
    // Check that encodeURI and decodeURI don't throw URIError (which occurs with broken surrogates)
    assert.doesNotThrow(() => encodeURIComponent(chunk));
  }

  // Combined text should reconstruct the full original text without corruption
  assert.equal(chunks.join(""), textWithEmoji);
});

test("splitMessageUnicode splits at paragraph and sentence breaks when possible", () => {
  const paragraph1 = "First paragraph content that is relatively long.";
  const paragraph2 = "Second paragraph content following a double newline.";
  const combined = `${paragraph1}\n\n${paragraph2}`;

  const chunks = splitMessageUnicode(combined, 55);
  assert.equal(chunks.length, 2);
  assert.equal(chunks[0], paragraph1);
  assert.equal(chunks[1], paragraph2);
});

test("deliverTelegramText sends plain text chunks in order and records message IDs", async () => {
  const calls: { url: string; body: any }[] = [];
  let nextMessageId = 101;

  const mockFetch: typeof fetch = async (input, init) => {
    const body = JSON.parse(init?.body as string);
    calls.push({ url: input.toString(), body });
    return new Response(
      JSON.stringify({
        ok: true,
        result: {
          message_id: nextMessageId++,
          date: 1700000000,
          chat: { id: body.chat_id },
          text: body.text,
        },
      }),
      { status: 200, headers: { "Content-Type": "application/json" } }
    );
  };

  const text = "First chunk sentence.\n\nSecond chunk sentence.";
  const result = await deliverTelegramText({
    botToken: "mock-token",
    chatId: 777,
    text,
    chunkLimit: 25,
    fetchFn: mockFetch,
  });

  assert.equal(result.success, true);
  assert.equal(result.chunksSent, 2);
  assert.equal(result.totalChunks, 2);
  assert.deepEqual(result.sentMessageIds, [101, 102]);

  // Verify plain text was sent without parse_mode
  assert.equal(calls.length, 2);
  assert.equal(calls[0].body.text, "First chunk sentence.");
  assert.equal(calls[0].body.parse_mode, undefined);
  assert.equal(calls[1].body.text, "Second chunk sentence.");
  assert.equal(calls[1].body.parse_mode, undefined);
});

test("deliverTelegramText handles 429 retry_after within delivery budget", async () => {
  let attempts = 0;
  const sleptMs: number[] = [];

  const mockSleep = async (ms: number) => {
    sleptMs.push(ms);
  };

  const mockFetch: typeof fetch = async () => {
    attempts++;
    if (attempts === 1) {
      // First attempt returns 429 with retry_after 1s
      return new Response(
        JSON.stringify({
          ok: false,
          error_code: 429,
          description: "Too Many Requests: retry after 1",
          parameters: { retry_after: 1 },
        }),
        { status: 429, headers: { "Content-Type": "application/json" } }
      );
    }
    // Second attempt succeeds
    return new Response(
      JSON.stringify({
        ok: true,
        result: { message_id: 888 },
      }),
      { status: 200, headers: { "Content-Type": "application/json" } }
    );
  };

  const result = await deliverTelegramText({
    botToken: "mock-token",
    chatId: 777,
    text: "Retry test message",
    deliveryBudgetMs: 5000,
    fetchFn: mockFetch,
    sleepFn: mockSleep,
  });

  assert.equal(result.success, true);
  assert.equal(attempts, 2);
  assert.deepEqual(sleptMs, [1000]);
  assert.deepEqual(result.sentMessageIds, [888]);
});

test("deliverTelegramText stops retry when retry_after exceeds remaining delivery budget", async () => {
  const mockFetch: typeof fetch = async () => {
    return new Response(
      JSON.stringify({
        ok: false,
        error_code: 429,
        description: "Too Many Requests: retry after 30",
        parameters: { retry_after: 30 }, // 30s > 5s budget
      }),
      { status: 429, headers: { "Content-Type": "application/json" } }
    );
  };

  const result = await deliverTelegramText({
    botToken: "mock-token",
    chatId: 777,
    text: "Over-budget message",
    deliveryBudgetMs: 5000, // 5s budget
    fetchFn: mockFetch,
  });

  assert.equal(result.success, false);
  assert.equal(result.error?.code, "RATE_LIMIT_BUDGET_EXCEEDED");
  assert.equal(result.error?.retryAfter, 30);
});

test("deliverTelegramText keeps delivery failure distinct without throwing or masking errors", async () => {
  const mockFetch: typeof fetch = async () => {
    return new Response("Forbidden: bot was blocked by the user", {
      status: 403,
      headers: { "Content-Type": "text/plain" },
    });
  };

  const result = await deliverTelegramText({
    botToken: "mock-token",
    chatId: 777,
    text: "Blocked user message",
    fetchFn: mockFetch,
  });

  assert.equal(result.success, false);
  assert.equal(result.error?.code, "HTTP_403");
  assert.ok(result.error?.description.includes("blocked by the user"));
});
