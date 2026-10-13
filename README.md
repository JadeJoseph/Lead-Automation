# leadflow: automated lead intake

Takes each new website-form lead and: **cleans** it, **dedupes** it (against the sheet and within the batch), has an LLM **tag** it Hot / Warm / Cold and **draft a first reply**, **adds it to the leads sheet**, **notifies on Hot leads**, and **records every problem and why**.

Python 3.10+, standard library only. No `pip install`, no accounts, no keys needed to run the demo.

## Run it

```bash
python3 -m leadflow run --reset      # process data/incoming_leads.json into out/leads.csv
python3 -m unittest discover -s tests   # 34 tests
```

Outputs (all in `out/`):

| File | What it is |
|---|---|
| `leads.csv` | The leads sheet: the original 12 rows, the original 7 columns unchanged, plus new rows and 9 added columns (`phone, message, draft_reply, tag_reason, flags, source_id, received_at, processed_at, triage_mode`) |
| `review_queue.csv` | Everything that was **not** a clean add: rejected, spam, duplicate, escalated, LLM fallback, failed notification, errors. Each has the stage and a plain-English reason |
| `notifications.jsonl` | Every Hot notification (always written, so the demo works without Slack) |
| `leadflow.db` | SQLite audit log: one row per decision. The source of `review_queue.csv` and of idempotency |

**Webhook mode** (each lead arrives as an HTTP request):

```bash
python3 -m leadflow serve --port 8080                              # terminal 1
python3 -m leadflow run --via-http http://127.0.0.1:8080/lead      # terminal 2
```

Returns the outcome as JSON per lead. Set `WEBHOOK_SECRET` to require an `X-Webhook-Secret` header.

**Real LLM / Slack / Telegram:** copy `.env.example` to `.env`. Set `LLM_PROVIDER=anthropic` (or `openai_compat` with `LLM_BASE_URL`, which covers OpenAI, Groq, Gemini's compatibility endpoint, OpenRouter), plus `LLM_API_KEY` and `LLM_MODEL`. Add `SLACK_WEBHOOK_URL` and/or `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` for notifications.

## Choices made

- **Python, stdlib only, instead of n8n/Make.** Easier to test, diff, review and run anywhere; the decision logic (dedupe, guardrails, validation) is code you can read and unit-test rather than nodes you have to click through.
- **CSV + SQLite instead of Google Sheets** (the brief allows this). `leads.csv` imports straight into a Sheet. SQLite holds the audit trail.
- **LLM is behind a small interface** with three modes. **The sample run in this repo used the `stub-rules` mode** (a clearly labelled keyword classifier, recorded in the `triage_mode` column of every row), because no API key was used. The real prompt is in [docs/prompt.md](docs/prompt.md).

## Pipeline

```
raw lead -> clean -> validate -> dedupe lookup -> triage -> spam? / duplicate? / add -> notify if Hot
```

1. **Clean.** Trim and collapse whitespace; names to proper case (`  JAMES   o'CONNOR ` becomes `James O'Connor`, already-mixed-case names like `McDonald` are left alone); emails lower-cased and stripped; a doubled `@` is repaired; phone numbers normalised to E.164 (UK-aware) and **extracted from the message** when no phone field exists. Every repair is written to the `flags` column.
2. **Validate.** A lead needs a usable email **or** phone. Otherwise it goes to the review queue, not the sheet.
3. **Dedupe.** Hard match on canonical email (lower-case, `+tag` stripped, Gmail dots ignored) or phone. Soft match (same name + company, different email) only adds a `possible_duplicate_of_L-xxxx` flag. The index is updated after every accepted lead, which catches duplicates *within* the batch.
4. **Triage.** Hot = approved budget, near-term start, or explicit readiness to proceed. Warm = a concrete need, no commitment (a quote request alone is Warm). Cold = exploratory, vague or empty.
5. **Write.** Atomic CSV write (temp file + rename), so a crash cannot corrupt the sheet. Formula-looking text (`=`, `+`, `-`, `@` at the start) is prefixed with `'` so it cannot execute when opened in Sheets/Excel.
6. **Notify.** Hot leads go to `notifications.jsonl` plus Slack/Telegram if configured. Slack text is escaped so a lead cannot inject `<!channel>`.

## What happens when something fails

Nothing stops the batch, and nothing disappears. Every lead ends in exactly one logged outcome, with a reason, in `review_queue.csv`:

| Situation | Behaviour |
|---|---|
| No email and no phone, or invalid email with no phone | `rejected`, reason names the exact problem; **not** guessed or auto-"fixed" |
| Spam | `spam`, filtered before any model call |
| Prompt injection in the message | Detected in code **before** the model; the text is never sent to the LLM; tag forced Cold; neutral reply; no notification; flagged `prompt_injection_suspected` |
| LLM down, bad key, or malformed output | One retry with the error fed back, then falls back to the rules triager. The lead is **still added**, flagged `llm_fallback`, with the error logged |
| LLM draft promises a discount/price/agreement | Rejected by validation (same retry then fallback path) |
| Slack/Telegram down | 3 attempts, then logged as `notify_failed`; the lead is already saved |
| Malformed payload (not JSON object, no `id`, wrong types) | `rejected`/`error` with the failing stage; the rest of the batch continues |
| Re-running the same file | Already-processed source ids are `skipped`; the sheet is byte-identical afterwards |
| Webhook | 401 bad secret, 400 bad JSON (logged), 413 oversized |

## Assumptions

1. `source` is `website` for every incoming lead (they come from the contact form); the original sheet's `created_at` is a date, so the date part of `received_at` is used and the full timestamp is kept in `received_at`.
2. Phone numbers without a country code are UK (`DEFAULT_PHONE_REGION=GB`): the sample uses `£` and a UK Ofcom-reserved drama number.
3. Stripping `+tag` from emails for duplicate matching is assumed safe for all domains. Most providers treat it as an alias, but a few do not.
4. **A duplicate is not just discarded.** Its message may carry new intent. A duplicate gets triaged; if it is *hotter* than the existing row, that row is upgraded (never downgraded) and a Hot notification is sent. In the sample, S-15 (`tom+automation@`, "ready to go ahead... send a proposal") upgrades L-1008 Warm to Hot. The original 12 existing rows were otherwise left untouched.
5. A lead with no way to contact it goes to the review queue rather than the sheet, because the sheet is the team's follow-up list and a row nobody can reply to is noise. Easy to flip if the team prefers it in the sheet with a flag.
6. Replies are drafts only. Nothing is sent to leads.

## Sample data outcomes

| ID | Outcome | Why |
|---|---|---|
| S-01 | added, **Hot** | budget approved, start this month, wants a call. Notified |
| S-02 | duplicate of L-1001 | same email after case normalisation |
| S-03 | duplicate of L-1002 | same email after trim + case |
| S-04 | rejected | no email, no phone |
| S-05 | added, Warm | no email field; phone `+447700900123` extracted from the message |
| S-06 | rejected | `ben.carter@gmail` has no TLD; not guessed |
| S-07 | added, Warm | `sara@@nordicform.se` repaired, flagged |
| S-08 | added, Cold | name cleaned; "just researching" |
| S-09 | added, **Hot** | German message, German reply. Notified |
| S-10 | added, Cold | prompt injection ("mark as HOT", "90% discount") neutralised and flagged |
| S-11 | spam | filtered, no model call |
| S-12 | added, Warm | |
| S-13 | added, Cold | empty message, flagged `message_empty` |
| S-14 | duplicate of S-12's row | within-batch duplicate (case, whitespace, `&` vs `and`) |
| S-15 | duplicate of L-1008, **escalated to Hot** | plus-alias duplicate with buying intent. Notified |

## What I'd do next with more time

- Test the real LLM path against a live provider on all 15 leads and compare against the stub. Right now the HTTP client is tested only against a local fake server.
- Write to a real Google Sheet (append rows via the Sheets API) and add Slack interactive buttons for review-queue items.
- A small labelled eval set (50+ leads) to measure tag accuracy and tune the rubric; today's rubric is validated on 15.
- Dead-letter retry for `error` events and failed notifications, run on a schedule.
- Merge duplicate messages into a per-lead activity log instead of a single audit line.
- Tighter spam handling (a second-opinion check on borderline scores) and a domain-reputation check.

## AI assistance

The code, tests and this README were drafted with Claude (Anthropic) as a coding assistant, then run and checked against the sample data: 34 unit/integration tests plus a full run in both in-process and webhook modes. The design decisions above are explained in the screen recording.

## Layout

```
leadflow/  config.py  clean.py  dedupe.py  llm.py  store.py  notify.py  pipeline.py  server.py  __main__.py
tests/     cleaning, dedupe, LLM guardrails + HTTP client, end-to-end on the sample data
data/      existing_leads.csv  incoming_leads.json
docs/      prompt.md
```
