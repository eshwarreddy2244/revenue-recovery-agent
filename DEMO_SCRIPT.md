# Demo script — 60 seconds

Use this to walk judges through the strongest part of the project: the
safety gate actually *preventing* an action, not just logging a warning.

## 1. Set the stage (10s)

> "The risky part of any auto-recovery system is what happens when it
> shouldn't act — a customer who opted out, or a payment that's now
> disputed. We built a gate that every single step checks before it
> fires, not just once at the start."

## 2. Show the overview tab (15s)

Point at the metrics: gross recovered, net preserved, recovery rate by
bucket. Then point at **Blocked, stopped, and skipped cases** —
call out the `TOUCH_CAP_EXCEEDED` / `OPT_OUT` / `DISPUTE_RAISED` counts.

> "Out of every case in this batch, the gate blocked [N] of them
> automatically — no human had to catch these."

## 3. Run the dispute-halt demo (25s)

Go to the **Safety-gate demo** tab, click **Run dispute-halt demo**,
and narrate the JSON as it appears:

> "Step 1 — smart retry — runs and fails to recover the payment.
> Right after that, a dispute lands on this case. Step 2 would
> normally send a payment link... but watch: the gate re-checks
> *before* every step, catches the dispute, and blocks it.
> `stopping_rule_hit: DISPUTE_RAISED`. `plink_id: null` — the link
> was never created. The sequence stops here, on the record."

Then point at the green confirmation banner and (if a Groq key is
set) the generated Hinglish customer message underneath it.

## 4. Close (10s)

> "Diagnosis, the gate, and the sequencer are all deterministic,
> unit-tested Python — 47 tests, zero LLM calls in that path. The
> only place an LLM touches this system is optional: parsing messy
> free-text failure reasons, and writing the customer-facing copy
> you just saw. If Groq is down, both degrade to safe templates and
> nothing about the recovery logic changes."

## If asked "why not let the AI decide?"

> "Because the decision that matters here is binary and high-stakes —
> contact this person or don't. That's exactly the kind of decision
> you want to be boring, deterministic, and testable, not something
> a model infers per-request."
