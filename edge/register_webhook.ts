import { config } from "./config.ts";

export interface SetWebhookOptions {
  url: string;
  secretToken?: string;
  dropPendingUpdates?: boolean;
  allowedUpdates?: string[];
}

export async function setTelegramWebhook(
  botToken: string,
  options: SetWebhookOptions
): Promise<any> {
  if (!botToken) {
    throw new Error("TELEGRAM_BOT_TOKEN is required to register webhook.");
  }
  if (!options.url || !options.url.startsWith("https://")) {
    throw new Error("Webhook URL must be a valid HTTPS endpoint (Telegram requirement).");
  }

  const endpoint = `https://api.telegram.org/bot${botToken}/setWebhook`;
  const body: Record<string, any> = {
    url: options.url,
    drop_pending_updates: options.dropPendingUpdates ?? true,
    allowed_updates: options.allowedUpdates ?? ["message"],
  };

  if (options.secretToken) {
    body.secret_token = options.secretToken;
  }

  const response = await fetch(endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  const data = await response.json();
  return data;
}

export async function getTelegramWebhookInfo(botToken: string): Promise<any> {
  if (!botToken) {
    throw new Error("TELEGRAM_BOT_TOKEN is required.");
  }
  const endpoint = `https://api.telegram.org/bot${botToken}/getWebhookInfo`;
  const response = await fetch(endpoint);
  return await response.json();
}

export async function deleteTelegramWebhook(
  botToken: string,
  dropPendingUpdates: boolean = false
): Promise<any> {
  if (!botToken) {
    throw new Error("TELEGRAM_BOT_TOKEN is required.");
  }
  const endpoint = `https://api.telegram.org/bot${botToken}/deleteWebhook`;
  const response = await fetch(endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ drop_pending_updates: dropPendingUpdates }),
  });
  return await response.json();
}

// CLI runner
if (process.argv[1]?.endsWith("register_webhook.ts") || process.argv[1]?.endsWith("register_webhook.js")) {
  const command = process.argv[2] || "info";
  const targetUrl = process.argv[3] || process.env.WEBHOOK_URL || "";
  const token = config.telegramBotToken;
  const secret = config.telegramWebhookSecret;

  (async () => {
    try {
      if (command === "set") {
        if (!targetUrl) {
          console.error("Usage: register_webhook set <HTTPS_WEBHOOK_URL>");
          process.exit(1);
        }
        console.log(`Setting webhook to: ${targetUrl}`);
        const result = await setTelegramWebhook(token, {
          url: targetUrl,
          secretToken: secret,
          dropPendingUpdates: true,
          allowedUpdates: ["message"],
        });
        console.log("Response:", JSON.stringify(result, null, 2));
      } else if (command === "info") {
        console.log("Fetching webhook info...");
        const result = await getTelegramWebhookInfo(token);
        console.log("Webhook Info:", JSON.stringify(result, null, 2));
      } else if (command === "delete") {
        console.log("Deleting webhook...");
        const result = await deleteTelegramWebhook(token, false);
        console.log("Response:", JSON.stringify(result, null, 2));
      } else {
        console.log("Unknown command. Available commands: info, set <URL>, delete");
      }
    } catch (err: any) {
      console.error("Error:", err.message);
      process.exit(1);
    }
  })();
}
