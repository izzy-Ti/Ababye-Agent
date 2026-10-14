import type { EdgeConfig } from "./config.ts";
import { config as defaultConfig } from "./config.ts";

export interface TelegramUser {
  id: number;
  is_bot: boolean;
  first_name: string;
  username?: string;
}

export interface TelegramChat {
  id: number;
  type: "private" | "group" | "supergroup" | "channel";
}

export interface TelegramMessage {
  message_id: number;
  from?: TelegramUser;
  chat: TelegramChat;
  date: number;
  text?: string;
  caption?: string;
}

export interface TelegramUpdate {
  update_id: number;
  message?: TelegramMessage;
  edited_message?: TelegramMessage;
  channel_post?: TelegramMessage;
  callback_query?: any;
}

export interface NormalizedAssistantRequest {
  request_id: string;
  update_id: number;
  user_id: number;
  chat_id: number;
  message_id: number;
  text: string;
}

export interface AssistantRunResponse {
  request_id: string;
  status: "completed" | "error" | "in_progress";
  answer: string | null;
  error_code: string | null;
}

/**
 * Validates the Telegram secret token header against configured secret.
 */
export function verifyWebhookSecret(
  providedSecret: string | null | undefined,
  expectedSecret: string
): boolean {
  if (!expectedSecret) return true; // if secret is empty in dev, allow
  return providedSecret === expectedSecret;
}

/**
 * Normalizes an incoming update into an assistant run request.
 * Returns null if the update should be ignored (e.g. unauthorized sender, non-private chat, non-text).
 */
export function normalizeTelegramUpdate(
  update: TelegramUpdate,
  allowedUserId: number
): NormalizedAssistantRequest | null {
  const message = update.message;
  if (!message) {
    // Non-message updates (e.g. edited_message, inline queries) follow documented ignore policy
    return null;
  }

  // Must be from private chat
  if (message.chat.type !== "private") {
    return null;
  }

  // Must be from the allowed user ID
  if (!message.from || message.from.id !== allowedUserId) {
    return null;
  }

  const rawText = message.text || message.caption || "";
  if (!rawText.trim()) {
    return null;
  }

  return {
    request_id: `tg:${update.update_id}`,
    update_id: update.update_id,
    user_id: message.from.id,
    chat_id: message.chat.id,
    message_id: message.message_id,
    text: rawText.trim(),
  };
}

/**
 * Send typing chat action to Telegram using native fetch
 */
export async function sendTypingAction(
  botToken: string,
  chatId: number
): Promise<void> {
  if (!botToken) return;
  try {
    const url = `https://api.telegram.org/bot${botToken}/sendChatAction`;
    await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        chat_id: chatId,
        action: "typing",
      }),
    });
  } catch (err) {
    console.error("Failed to send typing action to Telegram:", err);
  }
}

/**
 * Send text message to Telegram using native fetch
 */
export async function sendTelegramMessage(
  botToken: string,
  chatId: number,
  text: string
): Promise<void> {
  if (!botToken) return;
  try {
    const url = `https://api.telegram.org/bot${botToken}/sendMessage`;
    await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        chat_id: chatId,
        text,
      }),
    });
  } catch (err) {
    console.error("Failed to send message to Telegram:", err);
  }
}

/**
 * Split long message text into safe chunks under Telegram's 4096 char limit
 */
export function splitMessage(text: string, limit: number = 4000): string[] {
  if (text.length <= limit) return [text];
  const chunks: string[] = [];
  let remaining = text;
  while (remaining.length > 0) {
    if (remaining.length <= limit) {
      chunks.push(remaining);
      break;
    }
    // Find last newline before limit to avoid splitting sentences awkwardly
    let splitIndex = remaining.lastIndexOf("\n", limit);
    if (splitIndex === -1 || splitIndex < limit / 2) {
      // Fallback to space
      splitIndex = remaining.lastIndexOf(" ", limit);
    }
    if (splitIndex === -1 || splitIndex < limit / 2) {
      // Hard split on character limit
      splitIndex = limit;
    }
    chunks.push(remaining.slice(0, splitIndex).trim());
    remaining = remaining.slice(splitIndex).trim();
  }
  return chunks;
}

/**
 * Call FastAPI backend assistant endpoint with an abort timeout
 */
export async function callBackendAssistant(
  payload: NormalizedAssistantRequest,
  config: EdgeConfig
): Promise<AssistantRunResponse> {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), config.backendTimeoutMs);

  try {
    const response = await fetch(`${config.backendUrl}/v1/assistant/run`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${config.edgeBackendSecret}`,
      },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });

    if (!response.ok) {
      const errorText = await response.text();
      return {
        request_id: payload.request_id,
        status: "error",
        answer: null,
        error_code: `HTTP_${response.status}: ${errorText}`,
      };
    }

    const data = (await response.json()) as AssistantRunResponse;
    return data;
  } catch (err: any) {
    const isAbort = err.name === "AbortError";
    return {
      request_id: payload.request_id,
      status: "error",
      answer: null,
      error_code: isAbort ? "DEADLINE_EXCEEDED" : (err.message || "BACKEND_UNAVAILABLE"),
    };
  } finally {
    clearTimeout(timeoutId);
  }
}

/**
 * Primary edge handler logic for an incoming webhook update
 */
export async function handleTelegramWebhook(
  secretHeader: string | null | undefined,
  body: any,
  cfg: EdgeConfig = defaultConfig
): Promise<{ statusCode: number; responseBody: Record<string, any> }> {
  // 1. Verify Secret Token
  if (!verifyWebhookSecret(secretHeader, cfg.telegramWebhookSecret)) {
    return { statusCode: 401, responseBody: { error: "Invalid webhook secret" } };
  }

  // 2. Normalize and filter update
  const normalized = normalizeTelegramUpdate(body as TelegramUpdate, cfg.allowedUserId);
  if (!normalized) {
    // Return 200 to acknowledge ignored / unauthorized update without processing
    return { statusCode: 200, responseBody: { status: "ignored" } };
  }

  // 3. Send typing action (non-blocking)
  sendTypingAction(cfg.telegramBotToken, normalized.chat_id).catch(() => {});

  // 4. Call backend assistant
  const result = await callBackendAssistant(normalized, cfg);

  // 5. Send reply to Telegram if completed
  if (result.status === "completed" && result.answer) {
    const chunks = splitMessage(result.answer);
    for (const chunk of chunks) {
      await sendTelegramMessage(cfg.telegramBotToken, normalized.chat_id, chunk);
    }
  } else if (result.status === "error") {
    const errMsg = `Error: ${result.error_code || "An unexpected error occurred."}`;
    await sendTelegramMessage(cfg.telegramBotToken, normalized.chat_id, errMsg);
  }

  return { statusCode: 200, responseBody: result };
}
