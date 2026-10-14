import http from "node:http";
import { config } from "./config.ts";
import { handleTelegramWebhook } from "./handler.ts";

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url || "/", `http://${req.headers.host || "localhost"}`);

  // Health check endpoint
  if (req.method === "GET" && (url.pathname === "/health" || url.pathname === "/healthz")) {
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ status: "ok", service: "edge" }));
    return;
  }

  // Telegram webhook endpoint
  if (req.method === "POST" && (url.pathname === "/webhook" || url.pathname === "/")) {
    let bodyText = "";
    req.on("data", (chunk) => {
      bodyText += chunk;
    });

    req.on("end", async () => {
      try {
        const body = bodyText ? JSON.parse(bodyText) : {};
        const secretHeader = req.headers["x-telegram-bot-api-secret-token"] as string | undefined;

        const result = await handleTelegramWebhook(secretHeader, body, config);
        res.writeHead(result.statusCode, { "Content-Type": "application/json" });
        res.end(JSON.stringify(result.responseBody));
      } catch (err: any) {
        res.writeHead(400, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ error: "Invalid JSON or request processing error", details: err.message }));
      }
    });
    return;
  }

  // Not found
  res.writeHead(404, { "Content-Type": "application/json" });
  res.end(JSON.stringify({ error: "Not Found" }));
});

const port = config.edgePort || 8080;
server.listen(port, () => {
  console.log(`[Edge] Telegram Edge server listening on port ${port}`);
});

export default server;
