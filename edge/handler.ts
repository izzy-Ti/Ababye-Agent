import crypto from "node:crypto";
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
  edited_channel_post?: TelegramMessage;
  callback_query?: any;
  inline_query?: any;
  chosen_inline_result?: any;
  poll?: any;
  poll_answer?: any;
  my_chat_member?: any;
  chat_member?: any;
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

export type IgnoreReason =
  | "not_an_object"
  | "missing_update_id"
  | "edited_message_ignored"
  | "channel_post_ignored"
  | "unsupported_update_type"
  | "missing_message"
  | "missing_chat"
  | "non_private_chat"
  | "missing_sender"
  | "unauthorized_user"
  | "empty_text";

export interface UpdateEvaluationResult {
  accepted: boolean;
  reason?: IgnoreReason;
  normalized?: NormalizedAssistantRequest;
}

/**
 * Validates the Telegram secret token header against configured secret using constant-time comparison.
 */
export function verifyWebhookSecret(
  providedSecret: string | null | undefined,
  expectedSecret: string
): boolean {
  if (!expectedSecret) {
    // If no secret configured in local dev, allow
    return true;
  }
  if (!providedSecret) {
    return false;
  }

  const expectedBuf = Buffer.from(expectedSecret, "utf-8");
  const providedBuf = Buffer.from(providedSecret, "utf-8");

  if (expectedBuf.length !== providedBuf.length) {
    return false;
  }

  return crypto.timingSafeEqual(expectedBuf, providedBuf);
}

/**
 * Evaluates an incoming update against security constraints and the documented ignore policy.
 * Handles missing fields safely without throwing.
 */
export function evaluateTelegramUpdate(
  update: any,
  allowedUserId: number
): UpdateEvaluationResult {
  if (!update || typeof update !== "object") {
    return { accepted: false, reason: "not_an_object" };
  }

  if (typeof update.update_id !== "number") {
    return { accepted: false, reason: "missing_update_id" };
  }

  // 1. Check for edited messages (Policy: ignored to prevent prompt injection and duplicate executions)
  if (update.edited_message || update.edited_channel_post) {
    return { accepted: false, reason: "edited_message_ignored" };
  }

  // 2. Check for channel posts (Policy: only private user chats are accepted)
  if (update.channel_post) {
    return { accepted: false, reason: "channel_post_ignored" };
  }

  // 3. Check for unsupported interactive/update types
  if (
    update.callback_query ||
    update.inline_query ||
    update.chosen_inline_result ||
    update.poll ||
    update.poll_answer ||
    update.my_chat_member ||
    update.chat_member
  ) {
    return { accepted: false, reason: "unsupported_update_type" };
  }

  const message = update.message;
  if (!message || typeof message !== "object") {
    return { accepted: false, reason: "missing_message" };
  }

  // 4. Validate chat existence and type
  const chat = message.chat;
  if (!chat || typeof chat !== "object") {
    return { accepted: false, reason: "missing_chat" };
  }

  if (chat.type !== "private") {
    return { accepted: false, reason: "non_private_chat" };
  }

  // 5. Validate sender identity
  const from = message.from;
  if (!from || typeof from !== "object" || typeof from.id !== "number") {
    return { accepted: false, reason: "missing_sender" };
  }

  if (allowedUserId !== 0 && from.id !== allowedUserId) {
    return { accepted: false, reason: "unauthorized_user" };
  }

  // 6. Validate message text content
  const rawText = message.text || message.caption || "";
  if (typeof rawText !== "string" || !rawText.trim()) {
    return { accepted: false, reason: "empty_text" };
  }

  const messageId = typeof message.message_id === "number" ? message.message_id : 0;

  return {
    accepted: true,
    normalized: {
      request_id: `tg:${update.update_id}`,
      update_id: update.update_id,
      user_id: from.id,
      chat_id: chat.id,
      message_id: messageId,
      text: rawText.trim(),
    },
  };
}

/**
 * Backward-compatible helper that normalizes an update or returns null if ignored.
 */
export function normalizeTelegramUpdate(
  update: TelegramUpdate,
  allowedUserId: number
): NormalizedAssistantRequest | null {
  const result = evaluateTelegramUpdate(update, allowedUserId);
  return result.accepted && result.normalized ? result.normalized : null;
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
  // 1. Verify Secret Token (401 if invalid)
  if (!verifyWebhookSecret(secretHeader, cfg.telegramWebhookSecret)) {
    return { statusCode: 401, responseBody: { error: "Invalid webhook secret" } };
  }

  // 2. Evaluate update against security constraints and ignore policy
  const evaluation = evaluateTelegramUpdate(body, cfg.allowedUserId);
  if (!evaluation.accepted || !evaluation.normalized) {
    // Return 200 with documented ignore reason to acknowledge Telegram without running model/backend
    return {
      statusCode: 200,
      responseBody: { status: "ignored", reason: evaluation.reason || "unsupported" },
    };
  }

  const normalized = evaluation.normalized;

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
