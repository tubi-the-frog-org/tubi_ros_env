# Proposal: multi-turn additions to the website contract

This lives in `pi_app/`, not `website/` — it's a proposal for the website side to review and
implement, not something already agreed or built there. `teammate_client.py` already sends these
today; the website's current server just broadcasts them to the browser, which doesn't yet know
what to do with them. Nothing breaks by sending them early, but it isn't a real integration until
this (or something like it) is actually built on the website side.

## What changed on the Pi side

The brain is no longer "one transcript in, one final result out." It's a real back-and-forth: Tubi
introduces himself, asks the guest to introduce themselves, asks a few open-ended questions, waits
for a confirmation word, then reveals a snack with percentages. Every one of those Tubi lines needs
to reach the browser and be spoken, not just the final result — and the browser needs to know when
to re-arm the mic for the next answer.

## New message: teammate -> server -> browser

```json
{"type": "speak", "payload": {"text": "Ah, greetings traveler! I am Tubi, three hundred years old... or was it just last Tuesday?"}}
```

Sent for every Tubi turn, intro through confirmation prompt. The browser should display this text
and treat it the same way it currently treats "Tubi is speaking" — and once speaking ends, go back
to listening for the guest's next answer (previously only "session end" meant "stop listening";
now the browser needs to listen again after every `speak`, until the final `display` result).

## Changed message: the final `display` result now carries percentages

```json
{
  "type": "display",
  "payload": {
    "kind": "result",
    "snack": "Chips",
    "percentages": {"chips": 62.0, "cookies": 25.0, "popcorn": 13.0}
  }
}
```

`percentages` is new — a personality-test-style breakdown across every snack, not just the winner.
Purely additive; existing code reading `payload.snack` still works.

## Not changed

`session:start` / `session:end` / `transcript` / `state` (`speaking: true|false`) all keep their
existing shape. `transcript.payload.text` is the only field the Pi side reads from a guest turn.

## Open question for whoever builds this

`transcript` messages still don't carry a `sessionId` (see the existing contract doc's example) —
the Pi side currently assumes only one conversation is ever active at a time (matching the site's
own one-guest-at-a-time lock) rather than needing one. If that assumption changes, `transcript`
would need a `sessionId` added too.
