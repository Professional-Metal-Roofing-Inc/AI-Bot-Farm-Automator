# Marketplace reply bot

This system answers Facebook Marketplace messages on a farm of Android phones.

## What it does, in plain language

A rack of phones is plugged into one PC. Each phone is signed in to its own
Facebook account and has its own Marketplace listings.

Every 10–20 minutes the system visits each phone in turn and, for each phone:

1. Opens Facebook and finds the Marketplace inbox.
2. Opens each unread conversation and reads the messages on screen.
3. Asks an AI what to reply, giving it the company's business rules, the
product the conversation is about, and today's date and time.
4. **If the answer is fully covered by the business rules** — a documented
price, the address, opening hours, a colour we stock — it types the reply
and sends it.
5. **If it is not** — a quote, a discount request, current stock, a complaint,
anything the rules do not answer — it sends nothing. It puts the
conversation in the **review app** for a human.

A person opens the review app, reads the conversation, edits the suggested
reply if needed, and approves it. The bot then goes back to that phone, finds
the conversation and sends the approved text.

The bot never invents a price, never negotiates, and never answers a question
the business rules do not cover. When in doubt it asks a human.

## The three programs

|File|What it is|
|-|-|
|`farm\_runner.py`|The rota. Loops over the phones, runs the bot on each, waits, repeats.|
|`draft\_reply.py`|The bot. Drives one phone: navigation, reading, AI, sending, delivering approvals.|
|`review\_service.py`|The review app. A small web app at `http://127.0.0.1:8770` where a human approves replies.|

`farm\_runner.py` starts `draft\_reply.py` as a separate program for each phone,
so one bad phone cannot take down the whole rota.

## Running it

All commands are run from `C:\\Users\\newac\\farm automator\\phone-ai`.

### The rota (this is the thing that must be running)

```powershell
.\\.venv\\Scripts\\python.exe farm\_runner.py --watch --service-timeout 300
```

It services every enabled phone, then waits 10–20 minutes and goes again.
`Ctrl+C` stops it after the phone it is currently on.

Only one rota can run at a time; a second one refuses to start and says which
process holds the lock. If a run is killed hard, the next start clears the
stale lock by itself.

### The review app

It starts automatically at logon via a Windows scheduled task called
**PhoneFarmReviewApp**, and restarts itself if it crashes. To drive it by hand:

```powershell
schtasks /run /TN PhoneFarmReviewApp     # start
schtasks /end /TN PhoneFarmReviewApp     # stop
```

Open it at `http://127.0.0.1:8770/?token=<REVIEW\_SERVICE\_SECRET>` — the token
is stored in the browser, so that is a one-time URL. It listens on this PC
only and is not reachable from other machines or phones.

Its log is `review\_service.log`.

### Checking one phone without touching the rota

```powershell
.\\.venv\\Scripts\\python.exe farm\_runner.py --phone R5CT43FH66Z --timeout 150
```

Navigation only: it opens Marketplace and counts unread conversations. No AI,
no typing. Run this on a phone you have just added or just fixed.

### Other commands

`farm\_runner.py`:

|Command|What it does|
|-|-|
|`--watch`|The rota. `--cycles 1` does a single pass.|
|*(no flag)*|Readiness check of every enabled phone.|
|`--phone SERIAL`|Limit either of the above to one phone.|
|`--discover \[--replace]`|Rebuild `phones.json` from the plugged-in phones.|
|`--service-timeout N`|Seconds allowed per phone in the rota (default 600).|
|`--min-interval-minutes` / `--max-interval-minutes`|The wait between rounds (default 10–20).|

`draft\_reply.py` (needs `DEVICE\_SERIAL` set, e.g. `$env:DEVICE\_SERIAL="R5CT43FH66Z"`):

|Command|What it does|
|-|-|
|`--service-once`|One full cycle on that phone. This is what the rota runs.|
|`--check-phone`|Navigation-only readiness check.|
|`--deliver-approved \[--dry-run]`|Send replies approved in the app. `--dry-run` finds and checks but sends nothing.|
|`--history`|The last 20 conversations the bot handled.|
|`--test-review-app`|Post a test card to the review app. No phone, no AI.|
|`--open-marketplace`, `--inspect-listing`, `--inspect-unread`|Diagnostics for navigation problems.|

## Settings (`.env`)

|Setting|Meaning|
|-|-|
|`OPENROUTER\_API\_KEY`|AI provider key. Required.|
|`OPENROUTER\_MODEL`|Override the AI model. Optional.|
|`DEVICE\_SERIAL`|Which phone a hand-run command drives. The rota sets this itself.|
|`AUTO\_SEND\_ENABLED`|`true` = safe replies are sent automatically. `false` = everything goes to the review app instead.|
|`AUTO\_SEND\_CONFIDENCE`|How sure the AI must be to send on its own (currently `0.90`). Below it, a human reviews.|
|`APPROVED\_DELIVERY\_ENABLED`|`true` = approved replies are delivered during the rota. `false` = only by the `--deliver-approved` command.|
|`REVIEW\_SERVICE\_URL`|Where the review app is (`http://127.0.0.1:8770`).|
|`REVIEW\_SERVICE\_SECRET`|Shared password for the review app. Same value everywhere.|
|`APPROVAL\_EXPIRY\_HOURS`|An approval not delivered within this many hours goes back to the queue (default 6).|
|`SENDING\_TIMEOUT\_MINUTES`|A delivery that never reports back is flagged after this long (default 20).|



## The phones

`phones.json` lists every phone: its serial, its position label, and whether
it is in the rota.

```json
{ "serial": "R5CT43FH66Z", "name": "Phone 1", "enabled": true }
```

* **`name`** is the position on the rack. It is what the review app shows on
each card, so it must match the label on the physical phone.
* **`enabled`** decides whether the rota visits it. Set it to `false` to take a
phone out without unplugging it.

To add a phone: plug it in, add it to `phones.json` (or run `--discover --replace`), run `--check-phone` on it once, then set `enabled` to `true`.
The first cycle on a brand-new phone is slow because a helper app has to be
installed on it; see `TROUBLESHOOTING.md`.

To renumber a phone, change `name`. Nothing else needs updating.

## Where everything is kept

|File|What it holds|
|-|-|
|`review.db`|The review app's cards: conversations, suggested replies, decisions, history.|
|`agent\_history.db`|Every conversation the bot handled, and which chats it has already checked.|
|`farm\_state.json`|Progress of the current round.|
|`farm\_service\_results.json`|Result of each phone cycle, with the last 2 KB of any error.|
|`farm\_check\_results.json`|Result of the last readiness check.|
|`review\_outbox.jsonl`|Conversations saved while the review app was unreachable. Sent automatically later. Normally absent.|
|`diagnostics/`|Screenshot, screen dump and summary saved whenever a phone cycle fails.|
|`review\_service.log`|Review app log.|
|`business\_rules.txt`|The facts the AI is allowed to state. **Edit this to change what the bot can answer.**|

## Daily check

```powershell
.\\.venv\\Scripts\\python.exe -c "import json;s=json.load(open('farm\_state.json'));print(s\['round\_completed\_at'], 'completed', s\['round\_completed'], 'failed', s\['round\_failed'])"
```

A healthy round is every phone completed and none failed. If a phone keeps
failing, `TROUBLESHOOTING.md` lists what each failure means.

Also worth a daily glance: the review app's **Needs attention** list. Those are
real customers waiting on a human.

## Further reading

* `HOW\_IT\_WORKS.md` — the flow in detail, and why each safety check exists.
* `TROUBLESHOOTING.md` — every failure seen so far and what to do about it.

