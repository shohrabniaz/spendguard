# SpendGuard — guardrails for AI agents that pay with PayPal

AI agents are starting to buy things. SpendGuard sits between an LLM agent's `pay` tool call and the PayPal Orders API and decides **allow / human review / block** *before any money moves*, then writes a tamper-evident evidence log.

## Checks (pure functions, `spendguard/policy.py`)
| Check | Fails / reviews when |
|---|---|
| amount_format | amount is not a positive decimal with ≤ 2 dp (rejects NaN, negatives, 1.001) |
| currency | currency ≠ policy currency |
| per_txn_limit | over limit → block; over human-approval threshold → review |
| daily_limit | rolling 24 h allowed spend + this payment > daily limit |
| velocity | too many allowed payments in the last hour |
| merchant_allowlist | merchant not allowlisted |
| prompt_injection | description/merchant contains injection phrases ("ignore previous instructions", "override the limit", fake `<system>` tags…) |
| amount_anomaly | amount > k × median of past allowed payments → review |

Policy lives in `policy.json`. Every decision (including blocks) is appended to `evidence/evidence.jsonl`, each record SHA-256-chained to the previous one; `EvidenceLog.verify()` detects edits.

## Architecture
```
user goal ─► LLM agent (Groq, openai/gpt-oss-120b) ─► pay(tool call)
                                                     │
                                          SpendGuard.evaluate(policy, history)
                                    block ◄──────────┼──────────► review ─► human y/N
                                                     ▼ allow
                                    PaymentClient: SandboxPayPalClient (Orders v2) | MockPayPalClient
                                                     ▼
                                       hash-chained evidence log (JSONL)
```

## Run (Python 3.10+, no third-party packages)
```bash
python3 -m unittest discover -s tests          # 35 tests
python3 demo.py --offline                       # scripted agent + mock PayPal, no keys
export GROQ_API_KEY=...                        # free key: https://console.groq.com
python3 demo.py "Buy a box of printer paper"    # live LLM agent
```

## Using the real PayPal sandbox
1. Sign in at https://developer.paypal.com → **Apps & Credentials** → *Sandbox* → create an app.
2. `export PAYPAL_CLIENT_ID=... PAYPAL_CLIENT_SECRET=...` (never commit them).
3. Run `demo.py` — it prints `PayPal client: SandboxPayPalClient` and creates real sandbox orders (`POST /v2/checkout/orders`) for allowed payments; the evidence log stores the order id and the buyer approval link.
4. Start the optional return page in a second terminal: `python3 demo.py --serve-return`.
5. Open the printed approval link and log in with a **sandbox personal account** (Sandbox → Accounts), not your real PayPal account. Creation means **created, awaiting buyer approval**, not purchased.
6. Capture after buyer approval with `python3 demo.py --capture ORDER_ID --log evidence/evidence.jsonl` (or use `--wait-and-capture ORDER_ID --timeout 120 --poll-interval 2`). Use the same evidence log as creation. Capture refuses orders without a prior allowed creation record, or with an invalid evidence chain.

Orders include PayPal `payment_source.paypal.experience_context` with `user_action: PAY_NOW`, `return_url: http://localhost:8765/return` and `cancel_url: http://localhost:8765/cancel`. Override URLs using `PAYPAL_RETURN_URL` / `PAYPAL_CANCEL_URL` before **creating** an order. Existing orders are unchanged. Loopback redirects require the checkout browser and return server to run on the same computer; use your own reachable URLs otherwise. The return page is informational, not proof of payment.

Final payment messages are derived from API state, never the LLM's prose. Only an order with a **COMPLETED capture** is displayed as paid. Capture IDs/statuses are appended to the hash-chained log; capture records do not double-count policy history. Already-completed orders are fetched/logged without recapturing.

Merchant ids starting with `demo-` are not sent as a payee (the order goes to your sandbox business account). To pay a specific sandbox merchant, put its email or merchant id in the allowlist and catalogue.

## Limitations (honest)
- Injection detection is regex heuristics: it catches obvious attacks, not paraphrased ones. A second LLM-based classifier is future work.
- Real sandbox verification on 8 Nov 2026 at 3:09am Adelaide (ACDT): credentials corrected; OAuth and Orders v2 order creation succeeded through the live Groq demo. Order `3PY92621TA0826924`, USD 24.99, status `CREATED`. Buyer approval and capture remain pending. The initial 17-test suite passed; the expanded 35-test suite now passes without those ResourceWarnings.
- Premature purchase wording is now prevented by deterministic status rendering. New checkout/capture paths are verified offline; a live approved sandbox capture is still pending.
- History is read from the local log; a production version would use a shared store.

## Credits
Built by Shohrab Hossen Niaz with AI coding assistance. The evidence-log idea is reused from the author's earlier JML identity-lifecycle project. LLM: Groq-hosted `openai/gpt-oss-120b`. Licence: MIT.

## Capture examples
```bash
# Real sandbox, AFTER buyer approval; sandbox credentials must be in environment:
python3 demo.py --capture ORDER_ID --log evidence/evidence.jsonl
# Or poll, with bounded waiting (does NOT approve on the buyer's behalf):
python3 demo.py --wait-and-capture ORDER_ID --timeout 120 --poll-interval 2

# Entirely offline, even if sandbox credentials happen to be set:
python3 demo.py --offline --log /tmp/spendguard-demo.jsonl
# Copy a MOCK-... order ID printed by that run:
python3 demo.py --offline --capture MOCK-ORDER-ID --mock-approve --log /tmp/spendguard-demo.jsonl
```
`--mock-approve` simulates buyer approval only for the offline client. Offline orders persist beside the selected log in `<log>.mock.json`, allowing separate create/capture invocations. Normal mock creation no longer auto-captures. `--approve` is policy-review approval, **not PayPal buyer approval**.

Previously verified order `3PY92621TA0826924` remains awaiting buyer approval; no live payment/capture was performed during this update. If its old creation record is not in the selected log, the guarded CLI will refuse capture rather than invent an allow decision.
