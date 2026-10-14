export interface EdgeConfig {
  telegramBotToken: string;
  telegramWebhookSecret: string;
  allowedUserId: number;
  backendUrl: string;
  edgeBackendSecret: string;
  edgePort: number;
  callbackSigningSecret?: string;
  backendTimeoutMs: number;
}

export function loadConfig(env: Record<string, string | undefined> = process.env): EdgeConfig {
  const telegramBotToken = env.TELEGRAM_BOT_TOKEN || "";
  const telegramWebhookSecret = env.TELEGRAM_WEBHOOK_SECRET || "";
  const rawAllowedUserId = env.ALLOWED_USER_ID || "0";
  const allowedUserId = parseInt(rawAllowedUserId, 10);
  const backendUrl = (env.BACKEND_URL || "http://localhost:8000").replace(/\/+$/, "");
  const edgeBackendSecret = env.EDGE_BACKEND_SECRET || "";
  const edgePort = parseInt(env.EDGE_PORT || "8080", 10);
  const callbackSigningSecret = env.CALLBACK_SIGNING_SECRET || undefined;
  const backendTimeoutMs = parseInt(env.BACKEND_TIMEOUT_MS || "25000", 10);

  return {
    telegramBotToken,
    telegramWebhookSecret,
    allowedUserId,
    backendUrl,
    edgeBackendSecret,
    edgePort,
    callbackSigningSecret,
    backendTimeoutMs,
  };
}

export const config = loadConfig();
