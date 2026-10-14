export interface DeliveryOptions {
  botToken: string;
  chatId: number;
  text: string;
  deliveryBudgetMs?: number;
  chunkLimit?: number;
  fetchFn?: typeof fetch;
  sleepFn?: (ms: number) => Promise<void>;
}

export interface DeliveryResult {
  success: boolean;
  sentMessageIds: number[];
  chunksSent: number;
  totalChunks: number;
  error?: {
    code: string;
    description: string;
    retryAfter?: number;
  };
}

const defaultSleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

/**
 * Splits text into ordered chunks under the specified limit (default 4000 chars, below Telegram's 4096 limit),
 * strictly preserving Unicode boundaries (surrogate pairs, grapheme clusters, multi-byte code points).
 */
export function splitMessageUnicode(text: string, limit: number = 4000): string[] {
  if (!text || text.length === 0) return [];
  if (text.length <= limit) return [text];

  const segmenter = new Intl.Segmenter(undefined, { granularity: "grapheme" });
  const segments = Array.from(segmenter.segment(text));

  const chunks: string[] = [];
  let currentChunk = "";
  let currentChunkLength = 0;

  for (let i = 0; i < segments.length; i++) {
    const segment = segments[i].segment;
    const segLen = segment.length;

    if (currentChunkLength + segLen > limit) {
      if (currentChunkLength > 0) {
        // Look for optimal natural break point (paragraph, newline, or whitespace) in currentChunk
        let breakIndex = -1;
        const doubleNewline = currentChunk.lastIndexOf("\n\n");
        if (doubleNewline !== -1 && doubleNewline > limit / 3) {
          breakIndex = doubleNewline + 2;
        } else {
          const newline = currentChunk.lastIndexOf("\n");
          if (newline !== -1 && newline > limit / 3) {
            breakIndex = newline + 1;
          } else {
            const space = currentChunk.lastIndexOf(" ");
            if (space !== -1 && space > limit / 3) {
              breakIndex = space + 1;
            }
          }
        }

        if (breakIndex !== -1 && breakIndex < currentChunk.length) {
          const firstPart = currentChunk.slice(0, breakIndex).trimEnd();
          const remainder = currentChunk.slice(breakIndex).trimStart();
          chunks.push(firstPart);
          currentChunk = remainder + segment;
          currentChunkLength = currentChunk.length;
          continue;
        }

        // Clean boundary split
        chunks.push(currentChunk.trimEnd());
        currentChunk = segment;
        currentChunkLength = segLen;
      } else {
        // Individual grapheme cluster exceeds limit (extremely rare), push anyway
        chunks.push(segment);
        currentChunk = "";
        currentChunkLength = 0;
      }
    } else {
      currentChunk += segment;
      currentChunkLength += segLen;
    }
  }

  if (currentChunk.trim().length > 0) {
    chunks.push(currentChunk.trim());
  }

  return chunks;
}

/**
 * Delivers Telegram-safe plain text:
 * 1. Chunks final answer below 4096-char limit preserving Unicode boundaries.
 * 2. Delivers chunks in sequential order.
 * 3. Records returned Telegram message_ids for each chunk.
 * 4. Handles HTTP 429 Too Many Requests using retry_after within delivery budget.
 * 5. Isolates delivery failures from agent execution failures.
 */
export async function deliverTelegramText(options: DeliveryOptions): Promise<DeliveryResult> {
  const {
    botToken,
    chatId,
    text,
    deliveryBudgetMs = 15000,
    chunkLimit = 4000,
    fetchFn = fetch,
    sleepFn = defaultSleep,
  } = options;

  if (!botToken) {
    return {
      success: false,
      sentMessageIds: [],
      chunksSent: 0,
      totalChunks: 0,
      error: { code: "NO_BOT_TOKEN", description: "Telegram bot token is not configured" },
    };
  }

  const chunks = splitMessageUnicode(text, chunkLimit);
  if (chunks.length === 0) {
    return {
      success: true,
      sentMessageIds: [],
      chunksSent: 0,
      totalChunks: 0,
    };
  }

  const sentMessageIds: number[] = [];
  const startTime = Date.now();

  for (let i = 0; i < chunks.length; i++) {
    const chunk = chunks[i];
    let sent = false;

    while (!sent) {
      const elapsed = Date.now() - startTime;
      const remainingBudget = deliveryBudgetMs - elapsed;

      if (remainingBudget <= 0) {
        return {
          success: false,
          sentMessageIds,
          chunksSent: i,
          totalChunks: chunks.length,
          error: {
            code: "DELIVERY_BUDGET_EXCEEDED",
            description: `Delivery budget of ${deliveryBudgetMs}ms exceeded while sending chunk ${i + 1}/${chunks.length}`,
          },
        };
      }

      try {
        const url = `https://api.telegram.org/bot${botToken}/sendMessage`;
        const res = await fetchFn(url, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            chat_id: chatId,
            text: chunk, // Plain text initially (no parse_mode) to avoid parse errors
          }),
        });

        if (res.ok) {
          const data: any = await res.json();
          if (data && data.ok && data.result && typeof data.result.message_id === "number") {
            sentMessageIds.push(data.result.message_id);
          }
          sent = true;
          break;
        }

        // Handle 429 Rate Limiting
        if (res.status === 429) {
          const errData: any = await res.json().catch(() => ({}));
          const retryAfterSec = errData?.parameters?.retry_after ?? 1;
          const retryAfterMs = retryAfterSec * 1000;

          if (retryAfterMs <= remainingBudget) {
            console.warn(`[Telegram Delivery] 429 received, retrying after ${retryAfterSec}s...`);
            await sleepFn(retryAfterMs);
            continue; // Retry sending current chunk
          } else {
            return {
              success: false,
              sentMessageIds,
              chunksSent: i,
              totalChunks: chunks.length,
              error: {
                code: "RATE_LIMIT_BUDGET_EXCEEDED",
                description: `retry_after (${retryAfterSec}s) exceeds remaining delivery budget (${Math.round(remainingBudget / 1000)}s)`,
                retryAfter: retryAfterSec,
              },
            };
          }
        }

        // Other HTTP failures (e.g. 400 Bad Request, 403 Forbidden, 5xx)
        const errText = await res.text().catch(() => "Unknown error");
        return {
          success: false,
          sentMessageIds,
          chunksSent: i,
          totalChunks: chunks.length,
          error: {
            code: `HTTP_${res.status}`,
            description: errText,
          },
        };
      } catch (networkErr: any) {
        return {
          success: false,
          sentMessageIds,
          chunksSent: i,
          totalChunks: chunks.length,
          error: {
            code: "NETWORK_ERROR",
            description: networkErr.message || "Failed to reach Telegram API",
          },
        };
      }
    }
  }

  return {
    success: true,
    sentMessageIds,
    chunksSent: chunks.length,
    totalChunks: chunks.length,
  };
}
