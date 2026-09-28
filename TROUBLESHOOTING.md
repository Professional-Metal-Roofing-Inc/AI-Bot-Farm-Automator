# Troubleshooting

Every failure below has actually happened. Each entry gives what you see, what
it means, and what to do.

## Where to look first

```powershell
# how did the last round go?
.\.venv\Scripts\python.exe -c "import json;s=json.load(open('farm_state.json'));print(s['round_completed_at'],'completed',s['round_completed'],'failed',s['round_failed'])"

# which phones failed, and why
.\.venv\Scripts\python.exe -c "import json;[print(r['created_at'],r['name'],r['status'],(r.get('stderr_tail') or '')[-200:]) for r in json.load(open('farm_service_results.json'))['results'][-15:] if r['status']!='completed']"
```

Every failed cycle also writes three files to `diagnostics/`: a screenshot
(`.png`), the screen contents (`.xml`) and a summary (`.json`). **Open the
`.json` first** — it names the error and which Facebook screen the phone was
on, which is usually enough.

---

## Facebook is logged out

**You see** — every cycle on that phone fails with:

> Facebook opened, but no supported Marketplace conversation list could be
> reached. Facebook was closed and restarted first, so this phone probably
> needs a look.

and the diagnostics `.json` shows `com.facebook.katana / .LoginActivity`.

**Means** — the phone is sitting on Facebook's login screen. The bot already
closed and relaunched Facebook once and still found a login screen.

**Do** — sign that account back in on the phone by hand. Set `enabled` to
`false` in `phones.json` until you have, so the rota stops wasting a slot on
it. Then `--check-phone` before re-enabling.

---

## A brand-new phone times out on its first cycle

**You see** — one `timeout` after the full `--service-timeout` (600s by
default) the first time a newly added phone is serviced, then normal cycles
afterwards. The log may mention `apkutils is not installed`, which is harmless.

**Means** — a phone that has never been driven needs a helper app installed on
it, and that can take longer than the per-phone limit.

**Do** — nothing, if later cycles are fine. To avoid the noise when adding
phones, run `--check-phone` on each new phone once **before** enabling it.

---

## A phone fails every round on an unrecognised screen

**You see** — "no supported Marketplace conversation list could be reached",
but the diagnostics show a chat or a React screen, not the login screen.

**Means** — Facebook was left somewhere the navigation does not recognise.

**Do** — usually nothing: the bot now closes Facebook and retries once, which
clears it. If a phone keeps doing it, open the diagnostics `.xml` and check
which screen it is stuck on; a newer Facebook chat layout may need adding to
the navigation.

---

## A reply was sent but reported "could not be verified"

**You see** — in the review app, a card flagged `automatic_send_not_verified`:

> Facebook's Send button was clicked, but the outgoing reply could not be
> verified. Automatic processing stopped to prevent a duplicate.

**Means** — the bot could not prove the message left. Sometimes it genuinely
did not send; more often the phone renders the sent message with no readable
text (the chat shows only a timestamp), so there was nothing to confirm.

**Do** — open that chat on the phone and look. If the reply is there, reject
the card. If it is not, approve it.

**Safe either way**: if the reply did go out, delivery refuses to send it a
second time, because the inbox preview shows `You: …`.

If this starts happening on *every* send, something has changed in Facebook's
layout — check whether the inbox preview still says `You:`.

---

## The same reply was sent twice

**Means** — an unverified send that actually worked, then approved in the app
and delivered again. Both halves are now guarded: verification falls back to
the inbox preview, and delivery refuses a chat whose newest message is ours.

**Do** — if it happens again, capture the conversation and check what the
inbox row preview says for it; that is the signal both guards rely on.

---

## The AI answered about the wrong product

**You see** — a card whose listing line is not a product: "Edge panels",
"Marketplace chats and your privacy", "John started this chat. View buyer
profile", or a customer's name.

**Means** — the product name was read off the chat screen and picked up
something else. The bot now takes the product from the inbox row instead.

**Do** — check the card's label against the real listing. Older cards keep the
wrong name; the reply you approve is what gets sent, so the label is cosmetic.

---

## The AI quoted the wrong opening hours

**Means** — it had no idea what day it was and guessed. Every request now
carries the real local date and time.

**Do** — if it recurs, check that `CURRENT DATE AND TIME` is still in the
prompt and that the PC's clock and timezone are right.

---

## "The AI provider was unavailable (rate limit or outage)"

**You see** — cards flagged `ai_provider_failure`, with `Error code: 429` in
the reason.

**Means** — the AI provider throttled us. The bot retries after 15s and 45s
before giving up, and never sends a half-answer.

**Do** — check the OpenRouter account. This was chronic on the shared free
model and was fixed by adding our own provider key (BYOK), so requests use our
own quota. If it returns, confirm that key is still attached.

---

## A conversation is only a photo, a video or a voice note

**You see** — a card saying:

> [No readable text. The newest message is probably a photo, video or voice
> message. Open this chat on the phone to reply.]

**Means** — exactly that. The bot cannot read images or audio.

**Do** — reply on the phone yourself, then reject the card. Approving it will
not work: delivery cannot verify a chat it cannot read, and will refuse.

---

## The review app is not reachable

**You see** — `The review app is unreachable … Saved to review_outbox.jsonl`.

**Means** — the app is not running. **No conversation is lost**: they are saved
and sent on a later round.

**Do**

```powershell
schtasks /query /TN PhoneFarmReviewApp
schtasks /run /TN PhoneFarmReviewApp
Get-Content review_service.log -Tail 30
```

The outbox empties itself on the next cycle. It holds the newest 500 items.

---

## The rota will not start

**You see** — "Another farm_runner.py is already running (PID …)".

**Means** — the lock file is held. Deliberate: two rotas would drive the same
phones and double-reply.

**Do** — if that PID is really running, it is working; leave it. If the machine
was reset, the next start clears the stale lock by itself. `farm_runner.lock`
names the PID, its start time and its command.

---

## Replies are going to review that should be automatic

**Check, in order**

1. `AUTO_SEND_ENABLED` is `true`.
2. `AUTO_SEND_CONFIDENCE` — replies below it always go to review.
3. The card's risk flags. Any flag at all forces review.
4. `business_rules.txt` — if the answer is not in there, the AI is *supposed*
   to refuse. Add the fact to the rules rather than loosening the thresholds.

---

## Approved replies are not being sent

**Check**

1. `APPROVED_DELIVERY_ENABLED` — if `false`, approvals wait for
   `--deliver-approved`.
2. Whether the card expired: after `APPROVAL_EXPIRY_HOURS` it returns to the
   queue as `stale` rather than being sent late.
3. Whether it went `stale` for a good reason — the customer wrote again, or
   somebody already answered on the phone.
4. Whether the conversation is buried: delivery scrolls about 60 conversations
   deep, so a very old chat may not be found. The card then says so.

Watch one go out before trusting it:

```powershell
$env:DEVICE_SERIAL="R5CT43FH66Z"
.\.venv\Scripts\python.exe draft_reply.py --deliver-approved --dry-run   # checks, sends nothing
.\.venv\Scripts\python.exe draft_reply.py --deliver-approved             # really sends
```

---

## Cards show a serial instead of a position

**Means** — that serial is missing from `phones.json`, or its `name` is blank.

**Do** — add or fix the entry. New cards pick it up immediately; the bot reads
`phones.json` itself when the rota has not supplied a name.
