# Telegram Update Ignore Policy

## Objective
This policy defines how the Telegram Assistant Edge webhook filters and handles updates before any backend or LLM interaction occurs.

The assistant is strictly single-tenant and private. To protect the assistant from unauthorized usage, unnecessary resource consumption, prompt injection, and replay loops, incoming updates must be strictly inspected and filtered at the edge.

---

## Ignore Rules

### 1. Webhook Secret Validation (`X-Telegram-Bot-Api-Secret-Token`)
- **Condition**: The incoming HTTP request does not present a valid `X-Telegram-Bot-Api-Secret-Token` matching the configured `TELEGRAM_WEBHOOK_SECRET`.
- **Action**: Immediate HTTP `401 Unauthorized`.
- **Reason**: Protects the edge endpoint against forged or spoofed requests from non-Telegram senders. Zero application or model logic is executed.

---

### 2. Unauthorized Sender (`message.from.id !== ALLOWED_USER_ID`)
- **Condition**: The message sender ID does not match `ALLOWED_USER_ID`.
- **Action**: Return HTTP `200 OK` with `{ status: "ignored", reason: "unauthorized_user" }`.
- **Reason**:
  - The assistant is private to one authorized owner.
  - Returning HTTP 200 acknowledges receipt to Telegram so Telegram does not repeatedly retry sending the unauthorized update.
  - No model call or backend processing is performed.

---

### 3. Non-Private Chat (`message.chat.type !== "private"`)
- **Condition**: An update originates from a `group`, `supergroup`, or `channel`.
- **Action**: Return HTTP `200 OK` with `{ status: "ignored", reason: "non_private_chat" }`.
- **Reason**:
  - The assistant is designed exclusively for a private direct chat.
  - Processing group or channel updates exposes the assistant to multi-user crosstalk, unauthorized user prompt injection, and unwanted token usage.

---

### 4. Edited Messages (`update.edited_message`)
- **Condition**: An update contains an `edited_message` rather than a new `message`.
- **Action**: Return HTTP `200 OK` with `{ status: "ignored", reason: "edited_message_ignored" }`.
- **Reason**:
  - Allowing edits would allow prompt injection against past conversation context.
  - Editing a previously answered message would trigger duplicate model calls and potential tool side-effects.
  - Re-execution is disallowed; users must send a new message.

---

### 5. Unsupported Media and Empty Text
- **Condition**: A message has no `text` or `caption` (e.g. stickers, game updates, contact cards, dice, animations without text).
- **Action**: Return HTTP `200 OK` with `{ status: "ignored", reason: "unsupported_content" }`.
- **Reason**: The MVP supports text and text-captioned updates. Pure media without instructions triggers no model turns. (Voice notes are handled under Milestone 3 via explicit audio pipeline).

---

### 6. Other Unsupported Update Types
- **Condition**: The update contains `channel_post`, `edited_channel_post`, `inline_query`, `chosen_inline_result`, `callback_query`, `shipping_query`, `pre_checkout_query`, `poll`, `poll_answer`, `my_chat_member`, `chat_member`, `chat_join_request`, or `message_reaction`.
- **Action**: Return HTTP `200 OK` with `{ status: "ignored", reason: "unsupported_update_type" }`.
- **Reason**: The assistant only interacts via direct text messages. Non-message update types are dropped safely without error.

---

## Summary Table

| Update / Request Condition | HTTP Response | Resulting Action | Model Called? |
|----------------------------|---------------|------------------|---------------|
| Invalid Secret Token       | `401 Unauthorized` | Dropped | **No** |
| Sender != ALLOWED_USER_ID  | `200 OK` (`ignored`) | Dropped | **No** |
| Chat type != "private"     | `200 OK` (`ignored`) | Dropped | **No** |
| Edited message             | `200 OK` (`ignored`) | Dropped | **No** |
| Unsupported update type    | `200 OK` (`ignored`) | Dropped | **No** |
| Missing text/caption       | `200 OK` (`ignored`) | Dropped | **No** |
| Malformed message fields   | `200 OK` (`ignored`) | Dropped | **No** |
| Authorized private text    | `200 OK` (`completed`) | Processed | **Yes** |
