# Notes for Claude

Read `README.md`, `HOW_IT_WORKS.md` and `TROUBLESHOOTING.md` first. This file
only records things that are easy to get wrong here.

## The farm is usually live

`farm_runner.py --watch` is normally running, and it starts a **fresh**
`draft_reply.py` for every phone. So:

- **Deploy `draft_reply.py` atomically.** Write the new version elsewhere, then
  `Move-Item -Force` it into place. A half-written file becomes a failed cycle
  on a real phone. Editing in place mid-round has bitten us.
- Changes to `farm_runner.py` only take effect when the watch loop is
  restarted; it is already loaded in memory.
- `review_service.py` needs the app restarted (`schtasks /end` then `/run`).
- Before touching a phone by hand, wait for a gap between rounds — check
  `farm_state.json` (`round_completed_at`, `next_run_at`). The lock file tells
  you whether a rota is running.

## Verify against real data, not memory

Real screen dumps live in `diagnostics/*.xml`. They can be replayed offline
against the parsing functions with a small stub exposing `dump_hierarchy()` and
`window_size()`, which is how several bugs were diagnosed without touching a
phone. Prefer this to reasoning about what Facebook "probably" renders.

Both databases are safe to read directly (`review.db`, `agent_history.db`) and
are usually the fastest way to answer "did this actually happen?".

## Tests

There is no test runner. Test scripts live in the session scratchpad and are
run directly with the venv's Python. They stub the phone and the review app, so
they can run while the farm is live.

Two habits that matter:

- Tests that assert "the *old* code is broken" rot the moment the fix ships.
  Assert the required behaviour, not the difference.
- When faking the phone, make the fake behave like the real one — a fake send
  that does not add the message to the chat hides real duplicate bugs.

## Things that look like bugs and are not

- **The unread flag is unreliable.** Facebook clears it on its own schedule.
  Nothing may depend on it alone.
- **Some sent messages have no readable text**, only a timestamp. The inbox
  preview (`You: …`) is the fallback source of truth.
- **The first cycle on a new phone can time out** while a helper app installs.
- Names in `phones.json` are **rack positions**, not identifiers. They change.
  Serials identify phones.

## Safety rules for this codebase

- Never make it easier to send a message. Every guard in `HOW_IT_WORKS.md`
  exists because of a real incident; when unsure, fail closed and queue for a
  human.
- Never let the bot state a fact that is not in `business_rules.txt`.
- The review app is localhost-only on purpose. Do not bind it to `0.0.0.0`
  without being asked.
- Ask before sending anything to a real customer, including when testing.
  `--dry-run` exists for this.
