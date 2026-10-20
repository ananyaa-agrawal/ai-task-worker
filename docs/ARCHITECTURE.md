# Architecture notes

This expands on the README with the mechanics: what one turn looks like, how the policy gate decides, how
verification and repair interact, and what is recorded.

## One turn of the loop

```
messages (append-only)                              harness
──────────────────────                              ───────
user: "Task from your colleague: ..."
assistant: [thinking] [tool_use browser_open /mail]
                                                    executor: validate input → assess (read) → run
                                                    browser: goto, settle, snapshot, screenshot
user: [tool_result "URL: .../mail\n---\n| From | Subject | ..."]
assistant: [thinking] [tool_use browser_click e14] [tool_use remember ...]
                                                    executor: assess click → link → read → run
                                                    monitor: no repeated calls, no error streak
user: [tool_result ...] [tool_result ...] [text "[harness] note"]   ← only when the monitor has something to say
...
assistant: [tool_use finish {status, summary, claims}]
                                                    on_terminal → Verifier.verify(...)
user: [tool_result is_error "independent check did NOT confirm ... failed checks: ..."]   ← fail and repairs left
...
assistant: [tool_use finish ...] → verdict pass → run ends
```

Design points:
- **All tool results from one turn go back in one user message**, so the model keeps making parallel calls
  where they're safe (e.g. fill several fields, then snapshot).
- **Append-only history.** The assistant content, including thinking blocks, is passed back unchanged. Harness
  notes are added as text blocks after tool results, never by editing earlier turns. This keeps the prompt
  cache valid and the reasoning chain intact.
- **Observations are automatic.** Every browser action returns the resulting page, so the model never acts
  blind and never spends a turn just looking.
- **Stop reasons:** `tool_use` → execute. `end_turn` without a tool → nudge, up to 2 times, then stop as
  `no_progress`. `max_tokens` → tool calls are not executed and are returned as errors. `refusal` → the run ends
  cleanly; server-side fallbacks make this rare.

## The page snapshot

`tools/snapshot.js` walks the visible DOM and emits:
- headings as `#`/`##`, tables as `| a | b |` rows, list items as `- `;
- interactive elements as `[eN] kind "accessible name" extras`, with `data-tw-ref="eN"` set on the element.
  Inputs show their current value (password values masked), selects list their options, and buttons say whether
  they submit a GET or POST form;
- forms as `form (POST /erp/invoices):` so the model can see what will happen;
- `role=alert` / `role=status` messages tagged `[ALERT]` / `[STATUS]` and also lifted into a `Page notices:`
  header, together with the HTTP status of the main document when it is an error.

Refs are renumbered on every snapshot. A stale ref gives a clear error rather than clicking the wrong element.

## Policy gate

| Signal (from the live DOM element) | Risk | Default behaviour (`--approval high`) |
|---|---|---|
| Link, GET form, typing, selecting, reading files | read | run |
| POST form containing a password field | auth | run (also allowed for the verifier) |
| POST form submit / unknown JS button | write | run, recorded in `writes` |
| Control text, form action or field names match payment/delete/bank/approve… | high | **ask a human**; denied → error result to the model |
| URL outside the allowlist, or a harness `/__` endpoint | blocked | never run |

Modes: `all-writes` asks before any write. `never-ask` (unattended) turns approval-required into refusal. The
verifier's context is `read_only`, so anything above `auth` is refused.

The assessment runs *before* the action, against the actual element the model referenced. So the model can't
mislabel what it's about to click.

## Verification and repair

Input to the verifier:
- the user's task, verbatim;
- the worker's `finish` report: status, summary, claims (each with where to check), follow-ups;
- the worker's working memory: facts *with claimed sources*;
- the executor's log of state-changing actions, with the URL each one landed on.

It gets its own browser context (separate cookies, so it must sign in itself), read-only tools, and a
`submit_verdict` tool with itemised checks (`check / expected / observed / passed`). If it runs out of steps,
the verdict is `inconclusive`, never an implicit pass.

Repair: a `fail` verdict becomes the `finish` tool's result (an error with the failed checks), and the worker
continues with full context. After `max_repair_rounds` (2) the run ends as `verification_failed`. When the worker
reports `blocked`/`failed` and made no changes, verification is skipped (there is nothing to check).

## Recorded artefacts per run (`runs/<id>/`)

| File | Content |
|---|---|
| `events.jsonl` | Every event: run_started, plan, thinking, assistant_text, tool_call, policy, approval_request/result, question/answer, tool_result, memory, screenshot, harness_note, finish_attempt, verification_started/result, repair_round, llm_usage, run_finished |
| `report.md` | Human-readable result: summary, claims, verifier checks with screenshot evidence, facts with sources, changes made, human interactions, cost |
| `result.json` | Machine-readable result (used by the eval runner) |
| `shots/` | A JPEG after every browser action (worker and verifier sessions labelled separately) |

## Sandbox fault injection

`POST /__reset {"chaos": {...}}` (or the console dropdown, or `--chaos` on the CLI):

| Key | Effect |
|---|---|
| `create_fail_after_commit: N` | Next N invoice saves are committed, then answer 503 "may or may not have been processed" |
| `create_fail_before_commit: N` | Next N saves answer 503 without committing |
| `session_ttl_requests: N` | The LedgerLite session expires after N authenticated requests |
| `erp_get_503_every: N` | Every Nth LedgerLite page load returns 503 |

`GET /__state` exposes the full state (invoices, vendors, chat, audit log) for the grader. The worker's browser
is blocked from `/__*`.

## Extending to a new environment

1. Add the app to `environment.json` (name, URL, description, secret names) and its host to the allowlist
   (`TW_SANDBOX_URL` / `Config.extra_allowed_hosts`).
2. Add credentials to the secrets file.
3. Optionally add tools for APIs the app exposes (`tools/` + `Tool(..., assess=...)` so they pass through the
   gate).
4. Add eval tasks with ground-truth checks.

The loop, policy gate, verifier, memory, human channels and console don't change.
