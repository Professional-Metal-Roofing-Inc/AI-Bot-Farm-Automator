# How it works

Read `README.md` first for what the system is and how to run it. This document
is about the mechanics, and above all about **why the safety checks exist**.
Most of them were added after a real incident. Removing one will bring that
incident back.

## One phone cycle, start to finish

`farm_runner.py` runs `draft_reply.py --service-once` for a phone, with
`DEVICE_SERIAL` and `PHONE_NAME` set. That program then:

1. **Checks its configuration** — AI key, business rules, review app. It fails
   here rather than after driving the phone.
2. **Empties the outbox** — anything saved while the review app was down.
3. **Opens the Marketplace inbox.** If it cannot get there, it closes Facebook
   and tries once more from a clean launch.
4. **Delivers approved replies** for this phone, if that is switched on.
5. **Reads the inbox rows.** No unread conversations → closes Facebook, done.
6. **For each unread conversation:** opens it, reads the messages, works out
   which listing it belongs to, asks the AI, then either sends the reply or
   puts the conversation in the review app.
7. **Returns to the inbox** and moves to the next conversation.

A cycle ends as `no_unread_closed`, `queue_complete_closed`,
`unread_remaining` or `review_stopped`. Anything else is a failure, and a
screenshot plus screen dump is written to `diagnostics/`.

## Reading a conversation

Everything the bot knows about a chat comes from Android's accessibility tree —
the same data a screen reader uses. There is no API.

This is the fragile part of the system, and three rules were learned the hard
way:

- **Ignore anything that is not Facebook.** Other apps draw on top of the
  screen. Samsung's Edge Panel handle is a node labelled "Edge panels", and it
  was once read as the product name.
- **The message list starts under the listing card**, not at a fixed fraction
  of the screen. A reply directly below the card was invisible, which made the
  bot unable to see its own sent messages.
- **The product name comes from the inbox row**, which Facebook writes as
  `Customer · Listing`. Reading it off the chat screen produced "Marketplace
  chats and your privacy" and "View buyer profile", and the AI then answered
  about the wrong product.

Some messages cannot be read at all: photos, videos and voice notes, and on
some phones the bot's own sent messages appear as nothing but a timestamp.
Both cases are handled explicitly rather than treated as an empty chat.

## What the AI is told

For each conversation the AI receives the business rules, the current local
date and time, the listing title, and the visible conversation. It must return
JSON: an action (`reply`, `human_review`, `ignore`), an intent, whether the
answer needs live data, whether the business rules support it, a list of risk
categories, the customer-facing reply, a reason and a confidence.

The date is supplied because the AI has no other way to know it. Without it, it
guessed: asked "is your shop open today?" on a Friday, it decided the day was
Saturday and quoted Saturday's hours.

The reply is sent automatically only if **all** of this holds:

- the action is `reply`
- the intent is one of the safe kinds — documented product information, listing
  availability, an exact documented price, business or policy information,
  general delivery information
- there are no risk flags at all
- confidence is at or above `AUTO_SEND_CONFIDENCE`
- `AUTO_SEND_ENABLED` is true

Anything else goes to the review app. Quotes, discounts, current stock,
calculations, complaints and delivery-to-a-specific-address always go to a
human, by design.

## Sending, and knowing it was sent

The bot types the reply, confirms the text really is in the input box, presses
Send, and then has to prove the message left. It accepts any of:

1. a new outgoing message matching the reply
2. a new matching bubble on screen, with the input box now empty
3. a new "Delivered"/"Sent" marker, with the input box now empty
4. failing all of those, `You: <reply>` in the conversation's **inbox preview**

The fourth exists because some phones render a sent message with no readable
text at all. It is deliberately conservative: it is skipped when an identical
reply was already in the chat, because the preview would then be ambiguous.

If none of them hold, the bot reports the send as unverified and queues the
conversation for review. It never retries a send on its own.

## The review app

Each conversation becomes a card. Cards move through these states:

```
pending ──approve──> approved ──claim──> sending ──> sent
   │                     │                   │
   │                     │                   ├──> stale   (chat moved on; back to the queue)
   │                     │                   └──> failed  (delivery did not report back)
   ├──reject──────────> rejected
   └──newer item for the same chat──> superseded

auto_sent = a reply the bot sent on its own. History only; never actionable.
```

Open cards — `pending`, `stale`, `failed` — are the ones needing a decision.

Endpoints (all need the shared secret; the bot uses it too):

| Endpoint | Used by |
|---|---|
| `POST /review` | the bot, queueing a conversation |
| `POST /history` | the bot, recording a reply it sent on its own |
| `GET /api/items` | the web page |
| `POST /api/items/{id}/approve` · `/reject` | the web page |
| `GET /approved?serial=` | the bot, fetching work |
| `POST /api/items/{id}/claim` | the bot, taking one delivery |
| `POST /api/items/{id}/delivered` | the bot, reporting the outcome |
| `GET /healthz` | is it up |

## Delivering an approved reply

1. **Claim it.** `approved → sending`, and only one caller can win. This is what
   makes a double delivery impossible.
2. **Find the chat** by its conversation key, scrolling up to 20 screens
   (roughly the newest 60 conversations). Read or unread, either is fine.
3. **Open it and wait** until the messages stop changing. A half-loaded chat
   could hide the customer's newest message.
4. **Check it is still the right thing to send:**
   - the inbox preview shows `You: …` → **already answered**, do not send
   - the newest message is ours → **already answered**, do not send
   - the newest message is a newer one from the customer → **stale**, back to
     the queue with the new message attached
   - no messages from the expected customer → **wrong chat**, do not send
   - otherwise → send
5. **Report** `sent`, `stale` or `failed`, and return the inbox to the top.

Approvals expire after `APPROVAL_EXPIRY_HOURS` and go back to the queue rather
than being delivered late. A delivery that never reports back is marked
`failed` after `SENDING_TIMEOUT_MINUTES` and is **never retried automatically**,
because nobody knows whether it was sent.

## Why the same conversation is not answered twice

Six independent checks, from cheapest to last resort:

| Check | Stops |
|---|---|
| 15-minute dedup table | Re-opening a chat that Facebook still shows as unread after we handled it. |
| Conversation fingerprint | Re-processing an identical conversation. |
| "Latest message is not incoming" | Replying when our own reply is the newest message. |
| Inbox preview `You: …` | The same, on phones where our messages are unreadable on screen. |
| Claim before sending | Two deliveries of one approval. |
| Newest-message check before sending | Delivering into a chat that has moved on. |

The unread flag itself is **not** reliable: Facebook clears it on its own
schedule, sometimes immediately and sometimes minutes later. Nothing depends on
it alone.

## Failure handling

- A phone that fails leaves diagnostics and does not stop the round.
- A timed-out phone is killed along with the helper processes it started.
- The review app being down never blocks the rota: conversations go to
  `review_outbox.jsonl` and are sent on a later round.
- AI rate limits back off for 15s, then 45s, honouring the provider's own
  retry hint, before giving up.
- Only one rota may run at a time, enforced by a lock file holding its PID.

## Changing this code safely

- `read_messages()` decides what the AI sees, whether a send is verified and
  whether a chat looks answered. Changing its filters affects all three.
- `conversation_key()` must stay stable for the same chat in **both** Read and
  Unread states; delivery locates conversations by it.
- The dedup window is time-based, so lengthening it delays replies to follow-up
  messages.
- Anything user-visible in a reply must come from `business_rules.txt`.
