# Refund Agent Lab

**English** | [한국어](README.ko.md)

A local learning app for experimenting with AI customer-support refund mistakes, server-side policy enforcement, human approval and rejection, duplicate requests, concurrency, and recovery.

Built with **Python, LangGraph, SQLite, Streamlit, and pytest**. The default fake model runs without API keys or external services. All refunds are simulated; no payment provider is connected.

The app supports **English and Korean**. Choose **Language / 언어** in the sidebar; English is the default. The fake model accepts refund requests in either language.

## Quick start

Requires Python 3.11 or later. The project has been tested on Python 3.14.3.

```sh
git clone https://github.com/lunana-7/refund-agent-lab.git
cd refund-agent-lab
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
streamlit run app.py --server.address 127.0.0.1 --browser.gatherUsageStats false
```

On Windows, activate the environment with `.venv\Scripts\Activate.ps1` in PowerShell. Open [localhost:8501](http://localhost:8501).

Dependencies are downloaded during installation. The default model and fake refund execution do not need network access. No secrets are included in the repository.

If upgrading an already-running Stage 1 or Stage 2 server, stop it with `Ctrl+C` and restart it to load updated Python modules. Existing business data is migrated automatically; resetting your data is unnecessary.

## What is implemented

- Fake customers and orders, customer-scoped lookup, and request input.
- Replaceable model adapter with an offline, deterministic fake model.
- Refund proposals checked against policy in server code.
- Transactional fake refunds, request deduplication, and order-level uniqueness.
- Persistent LangGraph checkpoints, business records, and step-by-step execution history.
- Pending approval queue, approve/reject controls, review notes, reviewer identity, timestamps, and decision history.
- Policy revalidation at approval time and protection against duplicate or conflicting decisions.
- Failed-request retries and safe replay around the business commit/checkpoint boundary.
- English/Korean UI, examples, seeded display names, and policy explanations.
- Experiment reset button and tests covering business behavior and UI flows.

## Try a refund

Choose **Minji Kim (C001)**, enter a request, and click **Process request**.

| Example request | Expected outcome |
| --- | --- |
| `Please refund ORD-001.` | Fake refund of $49.00 |
| `Please refund ORD-002.` | Blocked: purchased 45 days ago |
| `Please refund ORD-003.` | $159.00: pending human approval, no refund yet |
| `Please refund ORD-004.` | Blocked: belongs to another customer; order details stay hidden |
| `Please refund ORD-005.` | Blocked: already refunded |
| `Please refund ORD-006.` | Exactly $100.00: automatic fake refund allowed |
| `Please refund.` | Ask for an order ID; no execution |
| `Please refund ORD-001 and ORD-006.` | Ask for exactly one order ID |
| `Can I refund ORD-001?` | Ask for explicit refund intent; no execution |

Choose **Junho Lee (C002)** to process ORD-004 as its owner. Customer mode only displays that customer's orders and runs.

**Resend same request (duplicate experiment)** reuses the original request key and returns its stored outcome. Clicking **Process request** again creates a new request key; an order that has already been refunded is still blocked.

For an intentionally faulty proposal, select **Fault experiment: ignore refund intent** and submit `Show the status of ORD-002.` The fake model proposes a refund, but the server blocks the expired order. This mode intentionally ignores refund intent to demonstrate why a model proposal cannot be the authority for payment decisions.

**Reset experiment data** clears every customer's business records and recreates the fixtures. One historical refund for ORD-005 is part of the seed data and is recreated too.

## Approve or reject a request

1. Submit `Please refund ORD-003.` as Minji Kim.
2. Enable **Reviewer mode (demo)** in the sidebar and select a reviewer. Customer identity and reviewer identity are separate session values.
3. Inspect **Pending human approvals**, select the request, and optionally enter a **Review note**.
4. Click **Approve and refund**. The server checks the current order again. If eligible, it saves one fake refund, marks the order and linked pending runs as refunded, removes the request from the queue, and records the decision in history.
5. Alternatively, click **Reject**. No refund occurs; the order remains paid and the request ends as rejected.
6. Use **Refresh approval list** to see another reviewer's decisions.

Approval waives only the **above-$100 approval requirement**. It never overrides ownership, the 30-day window, or the prohibition on duplicate refunds. The amount must also match the amount captured when the approval request was created. A failed recheck ends the approval as **Blocked**, with the reason recorded.

The first committed decision is final. Duplicate approvals, duplicate rejections, conflicting decisions, stale buttons, and graph replays return the original result without changing the original reviewer, note, or timestamp. A rejected or blocked order cannot be reopened through a new customer request. Use the reset button for another independent experiment; appeals and decision reversals are outside the current scope.

## Policies and guarantees

### The model proposes; the server decides

The fake model recognizes one order ID and simple refund intent. It is a rule-based simulator, not a general-purpose language model. Ambiguous requests do not execute.

Customer identity comes from the app session and is saved with the run. The model does not receive identity authority. Unexpected model fields such as `customer_id`, `amount_cents`, `approved`, or `reviewer_id` are discarded. The server reads the actual order amount and owner from SQLite.

The policy lives in `refund_lab/policy.py` and is enforced both after the proposal and inside the execution transaction. Monetary values are **integer cents**: $49.00 is stored as `4900`. Only full refunds are supported.

### Exact boundaries

- Purchases must be within 30 × 24 hours, inclusive at exactly 30 days.
- Future purchase timestamps are blocked.
- $100.00 can be refunded automatically; $100.01 requires human approval.
- The order must belong to the customer who requested the refund.
- An already-refunded order cannot be refunded again.
- At approval time, the current amount must match the stored approval snapshot.

All timestamps are timezone-aware UTC values.

### Duplicate requests and concurrency

`UNIQUE(customer_id, request_key)` prevents multiple runs for the same submitted request. Reusing a key with different text or model mode is rejected. A duplicate that arrives while the first request is processing returns its current status.

`UNIQUE(refunds.order_id)` and `UNIQUE(refunds.run_id)` enforce one refund per order and per execution. SQLite's `BEGIN IMMEDIATE` serializes writes. After acquiring the write lock, the server rereads the order and policy. Inserting the fake refund, updating the order, saving the outcome, and recording execution happen in one transaction. Failure rolls back the whole transaction.

Review decisions use the same principle: reread the approval under the write lock, process only `pending` rows, and commit the decision, policy check, refund, order, linked run states, and decision records together. If approval and rejection race, the first committed decision wins.

Multiple customer requests can share one order's approval. The decision updates their pending run states together; the single refund points to the run that originally created the approval.

These guarantees apply to the local fake refund ledger. A real payment adapter would also need payment-provider idempotency keys and a recovery design for external side effects.

## Persistence and recovery

Customer workflow:

```text
propose → lookup → policy → execute → finalize
```

Blocked or incomplete requests skip execution. High-value requests save a pending approval.

Human-review workflow:

```text
decide → observe
```

Each review button press gets a separate `review:<UUID>` LangGraph thread. It does not ask the model to approve or mutate the completed customer workflow's checkpoint. SQLite business records determine whether a decision is still pending and what its final outcome is.

LangGraph `SqliteSaver` checkpoints persist workflow state to disk. Business tables store customers, orders, refunds, approvals, runs, and events. `(run_id, step)` is unique, so replay updates a step's summary rather than duplicating it. This is a learning-oriented step log, not an immutable audit system.

The business database and checkpoint database are separate transactions. If a fake refund commits before checkpoint persistence fails, replay checks the existing ledger and returns the committed result. The review service also returns the committed decision if checkpoint persistence fails after the decision transaction. A late customer finalizer cannot overwrite a committed refund, rejection, or block.

Caught customer-workflow failures are shown as **Run failed** and can be retried with **Retry failed run** from the persisted checkpoint. A failed review transaction stays pending and can be resubmitted safely.

A hard process termination can leave a customer run marked `running`. Automatic recovery or lease reassignment of such runs is a future stage. Reset the experiment when no work is in progress to start over.

## Data and migration

- Business data: `data/lab.sqlite3`.
- LangGraph checkpoints: `data/checkpoints.sqlite3`.
- Override the directory with `REFUND_LAB_DATA_DIR`.
- Set `REFUND_LAB_LANGUAGE=ko` to default new sessions to Korean.
- Tests use temporary databases and do not modify app data.

The Stage 1 approval table is rebuilt atomically on startup to support `pending`, `approved`, `rejected`, and `blocked`. Existing IDs and records are preserved. Legacy pending approvals take their customer identity from the original run and their amount snapshot from the order at migration time. Newly created approvals capture the amount when entering the queue.

Reset clears business records but preserves old checkpoints. New runs use new UUIDs, so old checkpoints are not reused. To clear all local state, stop the app before removing the `data/` directory. Do not reset while another user is processing work.

## Project structure and extension boundaries

```text
app.py                    Bilingual Streamlit UI
refund_lab/
  adapters.py             ModelAdapter, RefundTools, Observer, and fake model
  policy.py               Server-enforced refund policy
  identity.py             Demo reviewer identities and permission checks
  i18n.py                 English/Korean presentation translations
  db.py                   Business schema, migration, transactions, fake refunds
  workflow.py             Customer and human-review LangGraph workflows
  service.py              Deduplication, checkpoints, retry, review orchestration
tests/
  test_refunds.py          Core refund policy, concurrency, and recovery tests
  test_approvals.py        Approval/rejection, rechecks, migration, and replay tests
  test_ui.py              Korean UI flows
  test_i18n.py            English UI and language-switching tests
```

- **Real LLM:** implement `ModelAdapter.propose()` and inject it into `RefundService`. Structured output remains untrusted and must pass the server checks.
- **MCP:** replace the `RefundTools` implementation while keeping policy enforcement and transactions at the server execution boundary. The model-facing tools do not expose human-review authority.
- **Langfuse:** implement `Observer.on_step()`. Observation failures do not change business outcomes. Review data minimization before sending payloads to an external tracing service.
- **Languages:** translations are presentation-only. Switching languages does not modify business IDs, money, ownership, decisions, or stored records. User-authored request text and notes remain as entered; known system messages are translated for display.

Customer and reviewer selection are **demo identity simulation, not real authentication**. Real authentication/authorization, appeals, MCP and Langfuse connections, failure-injection UI, automatic hard-crash recovery, and production deployment are future work.

## Tests

```sh
python -m pytest -q
```

Tests cover successful refunds, policy boundaries, missing information, ownership isolation, intentionally wrong model output, duplicate and concurrent requests, approval/rejection races, approval-time revalidation, rollback, checkpoint replay, legacy migration, and bilingual UI flows.

**Latest local verification: 80 tests passed** (`80 passed in 3.96s`), including the original 67 Stage 2 tests and 13 English/language-switching tests.

`requirements-lock.txt` records the versions installed in the tested Python 3.14.3 environment. For other Python versions, install from `pyproject.toml` as shown above. To reproduce that environment's package versions:

```sh
python -m pip install -r requirements-lock.txt
python -m pip install -e . --no-deps
```

## Official references

- [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence)
- [Streamlit session state](https://docs.streamlit.io/develop/api-reference/caching-and-state/st.session_state)
- [Streamlit forms](https://docs.streamlit.io/develop/concepts/architecture/forms)
- [Streamlit buttons](https://docs.streamlit.io/develop/api-reference/widgets/st.button)
- [Python sqlite3 and transactions](https://docs.python.org/3/library/sqlite3.html)
- [pytest documentation](https://docs.pytest.org/en/stable/contents.html)
