# Ledgr — Template, Onboarding-Confirmation & HITL-Edit Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Ledgr honour the client's chosen accounting software at export, confirm the registered profile back to the user, and turn the broken "Edit" button into a real review-and-edit loop that no longer spams the channel.

**Architecture:** Three independently-shippable phases on the existing ADK-2.2.0 + Slack-Bolt stack. The fixes are mostly *finishing* accepted decisions (ADR-0003 HITL, ADR-0005 per-target projection) plus one new UX decision (ADR-0007). Phase 1 seeds the full client profile into the run's `state_delta` in the runner (the coordinator's `before_agent_callback` does not propagate to the document lane). Phase 2 adds a profile-confirmation card + a read-only view command. Phase 3 finishes HITL: cards name their file, an `apply_decision_node` applies edits, and the Edit button opens a Block-Kit modal whose submission becomes a per-client Correction.

**Tech Stack:** Python 3.12, `google-adk` 2.2.0 (`Workflow` graph + `RequestInput`), `slack_bolt` AsyncApp + sync `WebClient`, Firestore (sessions + profiles), pytest. Test command: `.venv/bin/pytest -q`.

**Grounding ADRs:** docs/adr/0003 (HITL via RequestInput), 0004 (corrections not memory), 0005 (canonical schema → per-target projection; see 2026-06-14 addendum), 0007 (HITL review/edit Slack surface). Glossary: CONTEXT.md ([[Batch (Job)]], [[Review (HITL)]], [[Correction]]).

---

## Root-cause summary (verified live)

- **Wrong template:** channel `C0BA7NJ1C5Q` → client `client-97b148846c8f` has `accounting_software="Xero"` in Firestore, and `FirestoreClientStore.get_by_channel(...).to_state()["software"] == "Xero"` — yet the export wrote QBS columns. The profile is loaded only by the **coordinator's** `before_agent_callback`, whose `state` write does **not** propagate to `consolidate_node`, which then hits `state.get("software") or "qbs"` (`accounting_agents/nodes.py:458`). Same propagation gap blanks the COA → empty "Account Code / COA" column.
- **Edit does nothing:** `route_node(ctx)` (the gate's successor) takes no `node_input`, so the resumed `ApproveDecision`/`edits` is dropped. The `edit` Bolt action also resumes with `edits=None`. Slack forbids inline field editing (`input` blocks live only in modals / App Home).
- **Channel spam + unidentifiable cards:** each file is its own session posting its own status + review card; cards label by extracted identity ("I-Receipt #1"), never the uploaded filename.

---

## File Structure

**Phase 1 — honour accounting software**
- Modify: `accounting_agents/slack_runner.py` — seed profile into `state_delta`; soft-gate when no profile.
- Modify: `accounting_agents/nodes.py` — drop the `or "qbs"` silent default in `consolidate_node`; echo the target in `deliver_node`.
- Test: `tests/test_slack_runner.py`, `tests/test_nodes.py`.

**Phase 2 — confirm the registered profile**
- Create: `profile_summary_blocks(...)` in `app/blocks.py`.
- Modify: `app/slack_app.py` (`handle_onboarding_submit`, `handle_ledgr_command`), `app/commands.py` (`parse_ledgr_command`), `app/blocks.py` (`ledgr_help_blocks`).
- Test: `tests/test_app_onboarding.py`, `tests/test_app_commands.py`.

**Phase 3 — HITL review & edit (ADR-0007)**
- Modify: `accounting_agents/nodes.py` — new `apply_decision_node`; store an editable proposal at pause; graph edge insert.
- Modify: `accounting_agents/agent.py` — wire `apply_decision_node` into the spine.
- Modify: `accounting_agents/slack_runner.py` — name the file on the card; `edit` opens a modal; new `view_submission` handler; persist a Correction.
- Create: `invoice_edit_modal(...)` in `app/blocks.py`.
- Test: `tests/test_nodes.py`, `tests/test_slack_runner.py`, `tests/test_app_blocks.py`.

---

# PHASE 1 — Honour the client's accounting software

Outcome: a client onboarded as Xero gets the Xero template (and its COA), with the target echoed back; an unconfigured client is soft-gated instead of silently defaulting to QBS.

### Task 1: Seed the full client profile into the run state in the runner

**Files:**
- Modify: `accounting_agents/slack_runner.py` (the `state_delta` block at ~419 inside `process_file_event`; add a module-level helper + a `client_store` seam)
- Modify: `accounting_agents/nodes.py:458` (drop the `or "qbs"` default)
- Test: `tests/test_slack_runner.py`

- [ ] **Step 1: Write the failing test for the seeding helper**

Add to `tests/test_slack_runner.py` (mirror its fake-based style):

```python
def test_profile_state_delta_includes_software_and_coa():
    from accounting_agents.slack_runner import _profile_state_delta
    from invoice_processing.export.client_context import ClientContext, CoaAccount

    class _Store:
        def get_by_channel(self, channel_id):
            assert channel_id == "C1"
            return ClientContext(
                client_id="CL-1",
                client_name="Acme Client Pte. Ltd.",
                accounting_software="Xero",
                fye_month=10,
                coa=[CoaAccount(key="k", code="6010", description="Travel",
                                account_type="Expense", financial_statement="P&L",
                                nature="Dr", keywords=["travel"])],
            )

    delta = _profile_state_delta(_Store(), "C1")
    assert delta["software"] == "Xero"
    assert delta["client_id"] == "CL-1"
    assert len(delta["coa"]) == 1


def test_profile_state_delta_empty_when_no_profile():
    from accounting_agents.slack_runner import _profile_state_delta

    class _Store:
        def get_by_channel(self, channel_id):
            return None

    assert _profile_state_delta(_Store(), "C1") == {}
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/pytest tests/test_slack_runner.py::test_profile_state_delta_includes_software_and_coa -q`
Expected: FAIL — `ImportError: cannot import name '_profile_state_delta'`.

- [ ] **Step 3: Implement the helper + a module-level default store**

In `accounting_agents/slack_runner.py`, near the other module-level imports/helpers (after the imports block), add:

```python
from invoice_processing.export.client_context import FirestoreClientStore

#: Default client store for profile seeding (overridable in tests).
_DEFAULT_CLIENT_STORE = FirestoreClientStore()


def _profile_state_delta(client_store, channel_id: str) -> dict:
    """Return the client's ``to_state()`` keys for seeding the run, or ``{}``.

    The coordinator's ``before_agent_callback`` does not reliably propagate the
    profile into the document lane, so the runner seeds it directly at run start
    (alongside ``channel_id``). Empty dict means "no profile for this channel" —
    callers soft-gate on that. See ADR-0005 (2026-06-14 addendum).
    """
    ctx = client_store.get_by_channel(channel_id)
    if ctx is None:
        return {}
    return ctx.to_state()
```

- [ ] **Step 4: Run it to verify it passes**

Run: `.venv/bin/pytest tests/test_slack_runner.py -k profile_state_delta -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Write the failing test for soft-gate + seeding in `process_file_event`**

Add to `tests/test_slack_runner.py`:

```python
def test_process_file_event_softgates_when_no_profile():
    slack = FakeSlackClient()
    db = FakeFirestore()
    store = SlackLedgerStore(FakeFirestore(), opener=slack.opener())

    class _NoProfileStore:
        def get_by_channel(self, channel_id):
            return None

    runner = _FakeRunner([], _ledger_payload())  # should never run
    result = asyncio.run(
        process_file_event(
            runner=runner, ledger_store=store, db=db, slack_client=slack,
            channel_id="C1", file_id="F1", app_name="acc",
            download_fn=lambda c, f: b"%PDF-1.4 fake",
            source_filename="invoice.pdf", client_store=_NoProfileStore(),
        )
    )
    assert result["status"] == "no_profile"
    assert any("set up this client" in t.lower() for t in _posted_texts(slack))
```

- [ ] **Step 6: Run it to verify it fails**

Run: `.venv/bin/pytest tests/test_slack_runner.py::test_process_file_event_softgates_when_no_profile -q`
Expected: FAIL — `process_file_event() got an unexpected keyword argument 'client_store'`.

- [ ] **Step 7: Add `client_store` param, soft-gate, and merge the profile into `state_delta`**

In `accounting_agents/slack_runner.py`, change the `process_file_event` signature to accept `client_store=None`, and just before the `state_delta = {...}` block (~419) insert the seed + gate; then merge the seed into `state_delta`:

```python
    # (signature) add:  client_store=None,
    client_store = client_store or _DEFAULT_CLIENT_STORE
    profile_delta = _profile_state_delta(client_store, channel_id)
    if not profile_delta or not profile_delta.get("software"):
        _post_message(
            slack_client, channel_id,
            "I don't have this client set up yet — run */ledgr settings* to choose "
            "the accounting software and financial year, then re-drop the document.",
        )
        return {"status": "no_profile", "channel_id": channel_id, "file_id": file_id}

    state_delta = {
        "channel_id": channel_id,
        "file_id": file_id,
        "source_filename": source_filename,
        nodes.ARTIFACT_NAME_KEY: artifact_name,
        **profile_delta,
    }
```

- [ ] **Step 8: Drop the silent `or "qbs"` default in `consolidate_node`**

In `accounting_agents/nodes.py:458`, change:

```python
    software = state.get("software") or "qbs"
```
to:
```python
    software = state.get("software")  # seeded by the runner; get_exporter raises if missing
```

`get_exporter(None)` already raises `ValueError` for an empty target (the no-silent-default backstop). The runner's soft-gate prevents reaching it in practice.

- [ ] **Step 9: Run the full runner + nodes suites**

Run: `.venv/bin/pytest tests/test_slack_runner.py tests/test_nodes.py -q`
Expected: PASS. If a pre-existing nodes test relied on the `or "qbs"` default, update it to seed `state["software"] = "qbs"` in its `_base_state()` usage.

- [ ] **Step 10: Commit**

```bash
git add accounting_agents/slack_runner.py accounting_agents/nodes.py tests/test_slack_runner.py
git commit -m "fix(export): seed client profile into run state; no silent QBS default (ADR-0005)"
```

### Task 2: Echo the accounting-software target in the delivery summary

**Files:**
- Modify: `accounting_agents/nodes.py` (`deliver_node`, the two `summary = ...` lines)
- Test: `tests/test_nodes.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_nodes.py`:

```python
def test_deliver_echoes_software_target():
    ctx = FakeContext({
        nodes.LEDGER_ROWS_KEY: {
            "fy": "2026", "kind": "invoice", "software": "Xero",
            "batches": [{"sheet": "Purchase", "rows": [{"Total Amount": 10}]}],
        }
    })
    asyncio.run(nodes.deliver_node(ctx))
    summary = ctx.state[nodes.DELIVER_SUMMARY_KEY]
    assert "Xero" in summary
    assert "FY2026" in summary
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/pytest tests/test_nodes.py::test_deliver_echoes_software_target -q`
Expected: FAIL — `assert "Xero" in summary` (current summary omits the software).

- [ ] **Step 3: Implement — insert the software label into both summary branches**

In `accounting_agents/nodes.py` `deliver_node`, read the software once after `kind = payload.get("kind", "document")`:

```python
    software = payload.get("software") or ""
    target = f"{software} " if software else ""
```

Then change the bank branch:
```python
        summary = f"📒 Added {'; '.join(parts)} to your {target}FY{fy} ledger."
```
and the invoice branch:
```python
        summary = (
            f"📒 Added {n_rows} line{'s' if n_rows != 1 else ''} from "
            f"{len(batches)} document{'s' if len(batches) != 1 else ''} "
            f"to your {target}FY{fy} ledger."
        )
```

- [ ] **Step 4: Run it to verify it passes**

Run: `.venv/bin/pytest tests/test_nodes.py::test_deliver_echoes_software_target -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add accounting_agents/nodes.py tests/test_nodes.py
git commit -m "feat(deliver): echo the accounting-software target in the ledger summary"
```

---

# PHASE 2 — Confirm the registered profile

Outcome: after onboarding, the channel shows exactly what was registered (name · software · FYE · GST), and `/ledgr profile` re-shows it any time.

### Task 3: Profile-summary card + post it after onboarding

**Files:**
- Modify: `app/blocks.py` (new `profile_summary_blocks`; reuse `_MONTHS`)
- Modify: `app/slack_app.py` (`handle_onboarding_submit` — post summary before the COA prompt)
- Test: `tests/test_app_blocks.py` (create if absent), `tests/test_app_onboarding.py`

- [ ] **Step 1: Write the failing test for the block builder**

Create/append `tests/test_app_blocks.py`:

```python
from app.blocks import profile_summary_blocks


def _flat_text(blocks):
    return " ".join(
        b.get("text", {}).get("text", "")
        for b in blocks if isinstance(b.get("text"), dict)
    )


def test_profile_summary_shows_all_registered_fields():
    blocks = profile_summary_blocks({
        "client_name": "Acme Client Pte. Ltd.",
        "accounting_software": "Xero",
        "fye_month": 10,
        "gst_registered": False,
    })
    text = _flat_text(blocks)
    assert "Acme Client Pte. Ltd." in text
    assert "Xero" in text
    assert "October" in text          # fye_month 10 -> month name
    assert "Not GST-registered" in text
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/pytest tests/test_app_blocks.py -q`
Expected: FAIL — `ImportError: cannot import name 'profile_summary_blocks'`.

- [ ] **Step 3: Implement `profile_summary_blocks`**

In `app/blocks.py` (uses the existing `_MONTHS` list of `(num, name)`):

```python
def profile_summary_blocks(profile: dict) -> list:
    """Confirmation card summarising the client profile that was just registered."""
    name = profile.get("client_name") or "(unnamed client)"
    software = profile.get("accounting_software") or "—"
    fye_num = profile.get("fye_month")
    fye = next((n for num, n in _MONTHS if num == fye_num), "—")
    gst = "GST-registered" if profile.get("gst_registered") else "Not GST-registered"
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"✅ *Client registered: {name}*\n"
                    f"• Accounting software: *{software}*\n"
                    f"• Financial year-end: *{fye}*\n"
                    f"• GST status: *{gst}*"
                ),
            },
        }
    ]
```

- [ ] **Step 4: Run it to verify it passes**

Run: `.venv/bin/pytest tests/test_app_blocks.py -q`
Expected: PASS.

- [ ] **Step 5: Write the failing test that onboarding posts the summary**

Add to `tests/test_app_onboarding.py` (mirror its existing `handle_onboarding_submit` test style — a fake `client` recording `chat_postMessage` calls, a fake `store`):

```python
def test_onboarding_posts_profile_summary_then_coa_prompt():
    posted = []

    class _Client:
        def chat_postMessage(self, **kw):
            posted.append(kw)

    class _Store:
        def __init__(self): self.saved = None
        def get_by_channel(self, cid): return None
        def save_profile(self, doc): self.saved = doc
        def set_channel(self, cid, clid): pass

    body = _make_submit_body(  # existing helper building a view_submission body
        client_name="Acme Client Pte. Ltd.",
        fye_month="10", accounting_software="Xero", gst_value="no",
        channel_id="C1",
    )
    handle_onboarding_submit(body, ack=lambda: None, client=_Client(),
                             store=_Store(), id_factory=lambda: "CL-1")

    joined = " ".join(
        blk.get("text", {}).get("text", "")
        for call in posted for blk in call.get("blocks", [])
        if isinstance(blk.get("text"), dict)
    )
    assert "Client registered" in joined
    assert "Xero" in joined
    assert "Profile saved" in joined  # the COA prompt still follows
```

If `_make_submit_body` does not exist, build the body inline from the existing `_make_view_state(...)` helper plus `{"view": {... , "private_metadata": "C1"}, "team": {"id": "T1"}}`.

- [ ] **Step 6: Run it to verify it fails**

Run: `.venv/bin/pytest tests/test_app_onboarding.py::test_onboarding_posts_profile_summary_then_coa_prompt -q`
Expected: FAIL — only the COA prompt is posted; "Client registered" missing.

- [ ] **Step 7: Implement — post the summary before the COA prompt**

In `app/slack_app.py` `handle_onboarding_submit`, replace the final line:

```python
    client.chat_postMessage(channel=channel_id, blocks=coa_prompt_blocks())
```
with:
```python
    client.chat_postMessage(channel=channel_id, blocks=profile_summary_blocks(doc))
    client.chat_postMessage(channel=channel_id, blocks=coa_prompt_blocks())
```

Ensure `profile_summary_blocks` is imported alongside `coa_prompt_blocks` at the top of `app/slack_app.py`.

- [ ] **Step 8: Run it to verify it passes**

Run: `.venv/bin/pytest tests/test_app_onboarding.py -q`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add app/blocks.py app/slack_app.py tests/test_app_blocks.py tests/test_app_onboarding.py
git commit -m "feat(onboarding): confirm the registered client profile in-channel"
```

### Task 4: `/ledgr profile` view command

**Files:**
- Modify: `app/commands.py` (`parse_ledgr_command` — accept `"profile"`)
- Modify: `app/slack_app.py` (`handle_ledgr_command` — handle `"profile"`)
- Modify: `app/blocks.py` (`ledgr_help_blocks` — list the new command)
- Test: `tests/test_app_commands.py`

- [ ] **Step 1: Write the failing parser test**

Add to `tests/test_app_commands.py` (mirror existing parse tests):

```python
def test_parse_profile_subcommand():
    from app.commands import parse_ledgr_command
    assert parse_ledgr_command("profile").subcommand == "profile"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/pytest tests/test_app_commands.py::test_parse_profile_subcommand -q`
Expected: FAIL — "profile" falls through to "help".

- [ ] **Step 3: Implement — add `"profile"` to the recognised set**

In `app/commands.py` `parse_ledgr_command`, change:

```python
    if sub in ("settings", "export", "help"):
```
to:
```python
    if sub in ("settings", "export", "help", "profile"):
```

- [ ] **Step 4: Run it to verify it passes**

Run: `.venv/bin/pytest tests/test_app_commands.py::test_parse_profile_subcommand -q`
Expected: PASS.

- [ ] **Step 5: Write the failing handler test**

Add to `tests/test_app_commands.py` (mirror the existing `handle_ledgr_command` tests):

```python
def test_ledgr_profile_posts_summary():
    from app.slack_app import handle_ledgr_command
    from invoice_processing.export.client_context import ClientContext

    posted = []

    class _Client:
        def chat_postMessage(self, **kw): posted.append(kw)

    class _Store:
        def get_by_channel(self, cid):
            return ClientContext(client_id="CL-1", client_name="Acme Client Pte. Ltd.",
                                 accounting_software="Xero", fye_month=10, tax_registered=False)

    handle_ledgr_command(ack=lambda: None,
                         body={"channel_id": "C1", "text": "profile"},
                         client=_Client(), store=_Store())
    joined = " ".join(
        blk.get("text", {}).get("text", "")
        for call in posted for blk in call.get("blocks", [])
        if isinstance(blk.get("text"), dict)
    )
    assert "Acme Client Pte. Ltd." in joined and "Xero" in joined
```

- [ ] **Step 6: Run it to verify it fails**

Run: `.venv/bin/pytest tests/test_app_commands.py::test_ledgr_profile_posts_summary -q`
Expected: FAIL — `profile` currently routes to the help card.

- [ ] **Step 7: Implement — handle `"profile"` in `handle_ledgr_command`**

In `app/slack_app.py` `handle_ledgr_command`, add a branch before the `else` help branch:

```python
    elif cmd.subcommand == "profile":
        existing = store.get_by_channel(channel_id)
        if existing is None:
            client.chat_postMessage(
                channel=channel_id,
                text="No client is set up in this channel yet — run */ledgr settings*.",
            )
        else:
            profile = {
                "client_name": existing.client_name,
                "accounting_software": existing.accounting_software,
                "fye_month": existing.fye_month,
                "gst_registered": existing.tax_registered,
            }
            client.chat_postMessage(channel=channel_id, blocks=profile_summary_blocks(profile))
```

Import `profile_summary_blocks` in `app/slack_app.py` if not already.

- [ ] **Step 8: Update the help card**

In `app/blocks.py` `ledgr_help_blocks`, add a line after the `settings` line:

```python
                    "*/ledgr profile* — show this client's registered profile\n"
```

- [ ] **Step 9: Run the command suite**

Run: `.venv/bin/pytest tests/test_app_commands.py -q`
Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add app/commands.py app/slack_app.py app/blocks.py tests/test_app_commands.py
git commit -m "feat(commands): add /ledgr profile to view the registered client profile"
```

---

# PHASE 3 — HITL review & edit (ADR-0007)

Outcome: each review card names its document; pressing Edit opens a modal that actually edits account/tax/amount; the edit is applied to the ledger and remembered as a per-client Correction; a batch of N drops collapses to one Job summary.

> Edit shape used throughout: `edits = {"lines": [{"index": <int>, "account_code": <str?>, "tax_code": <str?>, "amount": <float?>}]}`, indexed by line position within the (single) invoice for this per-doc session.

### Task 5: Name the document on the review card + store an editable proposal at pause

**Files:**
- Modify: `accounting_agents/slack_runner.py` (where the approval card is posted from a paused interrupt — `_post_approval_card` / the interrupt-doc write)
- Modify: `app/blocks.py` (`approval_card_blocks` — accept and render a `doc_label`)
- Test: `tests/test_app_blocks.py`, `tests/test_slack_runner.py`

- [ ] **Step 1: Write the failing block test**

Add to `tests/test_app_blocks.py`:

```python
from app.blocks import approval_card_blocks


def test_approval_card_names_the_document():
    blocks = approval_card_blocks(
        summary="not reconciled (lines $51.49 vs $44.74 + GST)",
        op_id="OP1",
        doc_label="📄 Receipt-Hotel.pdf · Hotel Booking · $51.49",
    )
    head = blocks[0]["text"]["text"]
    assert "Receipt-Hotel.pdf" in head
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/pytest tests/test_app_blocks.py::test_approval_card_names_the_document -q`
Expected: FAIL — `approval_card_blocks() got an unexpected keyword argument 'doc_label'`.

- [ ] **Step 3: Implement — add an optional `doc_label` header line**

In `app/blocks.py`, change the `approval_card_blocks` signature and first section:

```python
def approval_card_blocks(summary: str, op_id: str, doc_label: str | None = None) -> list:
    header = ":mag: *Review needed before adding to the ledger*"
    if doc_label:
        header = f"{doc_label}\n{header}"
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"{header}\n{summary}"}},
        # ... unchanged actions block (Approve / Edit / Reject) ...
    ]
```

Keep the existing `actions` block exactly as-is.

- [ ] **Step 4: Run it to verify it passes**

Run: `.venv/bin/pytest tests/test_app_blocks.py::test_approval_card_names_the_document -q`
Expected: PASS.

- [ ] **Step 5: Write the failing test for `_doc_label_from_state`**

The label is built from the run state (filename + first invoice vendor + total). Add to `tests/test_slack_runner.py`:

```python
def test_doc_label_from_state():
    from accounting_agents.slack_runner import _doc_label_from_state
    state = {
        "source_filename": "Receipt-Hotel.pdf",
        "normalized_invoices": [
            {"vendor_name": "Hotel Booking", "total_amount": 51.49, "currency": "SGD"}
        ],
    }
    label = _doc_label_from_state(state)
    assert "Receipt-Hotel.pdf" in label
    assert "Hotel Booking" in label
    assert "51.49" in label
```

- [ ] **Step 6: Run it to verify it fails**

Run: `.venv/bin/pytest tests/test_slack_runner.py::test_doc_label_from_state -q`
Expected: FAIL — `ImportError: cannot import name '_doc_label_from_state'`.

- [ ] **Step 7: Implement `_doc_label_from_state` + pass it when posting the card**

In `accounting_agents/slack_runner.py`:

```python
def _doc_label_from_state(state: dict) -> str:
    """Human label tying a review card to its uploaded document."""
    fname = state.get("source_filename") or "document"
    invs = state.get("normalized_invoices") or []
    if invs:
        first = invs[0]
        vendor = first.get("vendor_name") or first.get("issuer_name") or ""
        total = first.get("total_amount")
        cur = first.get("currency") or ""
        money = f" · {cur} {total:,.2f}" if isinstance(total, (int, float)) else ""
        vend = f" · {vendor}" if vendor else ""
        return f"📄 {fname}{vend}{money}"
    return f"📄 {fname}"
```

Then where the approval card is posted (the function that reads the paused session state and calls `approval_card_blocks(summary, op_id)`), read the session state and pass `doc_label=_doc_label_from_state(state)`. Also persist the label on the interrupt doc (so the outcome update can reuse it): add `"doc_label": label` to the interrupt-doc dict written at pause time.

- [ ] **Step 8: Run the runner suite**

Run: `.venv/bin/pytest tests/test_slack_runner.py tests/test_app_blocks.py -q`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add accounting_agents/slack_runner.py app/blocks.py tests/test_app_blocks.py tests/test_slack_runner.py
git commit -m "feat(hitl): name the uploaded document on each review card"
```

### Task 6: `apply_decision_node` — apply edits / honour reject in the graph

**Files:**
- Modify: `accounting_agents/nodes.py` (new `apply_decision_node`; it receives the `ApproveDecision` as `node_input`)
- Modify: `accounting_agents/agent.py` (insert the node into the spine: `approval_gate → apply_decision_node → route_node → …`)
- Test: `tests/test_nodes.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_nodes.py`:

```python
def test_apply_decision_node_applies_line_edits():
    state = _base_state()
    state[nodes.NORMALIZED_KEY] = [{
        "invoice_number": "INV-1", "lines": [
            {"description": "Room", "account_code": None, "tax_code": "SR", "amount": 51.49}
        ],
    }]
    ctx = FakeContext(state)
    decision = {"decision": "edit", "edits": {"lines": [
        {"index": 0, "account_code": "6010", "tax_code": "ZR"}
    ]}}
    asyncio.run(nodes.apply_decision_node(ctx, decision))
    line = ctx.state[nodes.NORMALIZED_KEY][0]["lines"][0]
    assert line["account_code"] == "6010"
    assert line["tax_code"] == "ZR"
    assert ctx.state[nodes.APPROVAL_STATUS_KEY] == "edit"


def test_apply_decision_node_reject_clears_invoices():
    state = _base_state()
    state[nodes.NORMALIZED_KEY] = [{"invoice_number": "INV-1", "lines": []}]
    ctx = FakeContext(state)
    asyncio.run(nodes.apply_decision_node(ctx, {"decision": "reject"}))
    assert ctx.state[nodes.NORMALIZED_KEY] == []
    assert ctx.state[nodes.APPROVAL_STATUS_KEY] == "reject"


def test_apply_decision_node_autoapprove_passthrough():
    state = _base_state()
    state[nodes.NORMALIZED_KEY] = [{"invoice_number": "INV-1", "lines": []}]
    ctx = FakeContext(state)
    asyncio.run(nodes.apply_decision_node(ctx, None))  # no HITL → node_input is None
    assert ctx.state[nodes.NORMALIZED_KEY] == [{"invoice_number": "INV-1", "lines": []}]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/pytest tests/test_nodes.py -k apply_decision_node -q`
Expected: FAIL — `module 'accounting_agents.nodes' has no attribute 'apply_decision_node'`.

- [ ] **Step 3: Implement `apply_decision_node`**

In `accounting_agents/nodes.py` (place just before `route_node`):

```python
@node
async def apply_decision_node(ctx, node_input=None) -> Event:
    """Apply the human's ApproveDecision (resume node_input) to the run state.

    Auto-approved runs pass ``node_input=None`` and fall straight through. On
    ``edit`` the per-line corrections are written onto ``state[NORMALIZED_KEY]``
    before routing/consolidation; on ``reject`` the invoices are cleared so the
    consolidate/deliver spine produces nothing.
    """
    decision = node_input if isinstance(node_input, dict) else {}
    choice = decision.get("decision")
    if not choice:
        return Event(output={"decision": "auto_approved"})

    ctx.state[APPROVAL_STATUS_KEY] = choice

    if choice == "reject":
        ctx.state[NORMALIZED_KEY] = []
        return Event(output={"decision": "reject"})

    if choice == "edit":
        edits = (decision.get("edits") or {}).get("lines") or []
        invoices = ctx.state.get(NORMALIZED_KEY) or []
        if invoices:
            lines = invoices[0].get("lines") or []
            for e in edits:
                i = e.get("index")
                if isinstance(i, int) and 0 <= i < len(lines):
                    for field in ("account_code", "tax_code", "amount", "description"):
                        if e.get(field) is not None:
                            lines[i][field] = e[field]
            ctx.state[NORMALIZED_KEY] = invoices
    return Event(output={"decision": choice})
```

- [ ] **Step 4: Run it to verify it passes**

Run: `.venv/bin/pytest tests/test_nodes.py -k apply_decision_node -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Wire the node into the spine**

In `accounting_agents/agent.py` `document_workflow` edges, change the post-approval chain from:

```python
        (
            nodes.approval_gate,
            nodes.route_node,
            nodes.consolidate_node,
            nodes.deliver_node,
        ),
```
to:
```python
        (
            nodes.approval_gate,
            nodes.apply_decision_node,
            nodes.route_node,
            nodes.consolidate_node,
            nodes.deliver_node,
        ),
```

Update the module docstring spine comment (`-> approval_gate -> route_node -> …`) to include `apply_decision_node`.

- [ ] **Step 6: Run the nodes + an end-to-end runner test**

Run: `.venv/bin/pytest tests/test_nodes.py tests/test_slack_runner.py -q`
Expected: PASS. (The auto-approve passthrough keeps existing non-HITL tests green.)

- [ ] **Step 7: Commit**

```bash
git add accounting_agents/nodes.py accounting_agents/agent.py tests/test_nodes.py
git commit -m "feat(hitl): apply approve/edit/reject decision in the graph spine"
```

### Task 7: Edit button opens a Block-Kit modal; submission resumes with edits

**Files:**
- Create: `invoice_edit_modal(...)` in `app/blocks.py`
- Modify: `accounting_agents/slack_runner.py` (the `@async_app.action("edit")` handler → open modal; new `@async_app.view("ledgr_invoice_edit")` handler → build edits + resume)
- Test: `tests/test_app_blocks.py`, `tests/test_slack_runner.py`

- [ ] **Step 1: Write the failing modal-builder test**

Add to `tests/test_app_blocks.py`:

```python
from app.blocks import invoice_edit_modal


def test_invoice_edit_modal_prefills_and_carries_op_id():
    view = invoice_edit_modal(
        op_id="OP1",
        lines=[{"description": "Room", "account_code": "6010", "tax_code": "SR", "amount": 51.49}],
        coa_options=[("6010", "6010 — Travel"), ("6200", "6200 — Office")],
    )
    assert view["callback_id"] == "ledgr_invoice_edit"
    assert view["private_metadata"] == "OP1"
    # one input group per line (account + tax + amount) → at least 3 input blocks
    inputs = [b for b in view["blocks"] if b.get("type") == "input"]
    assert len(inputs) >= 3
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/pytest tests/test_app_blocks.py::test_invoice_edit_modal_prefills_and_carries_op_id -q`
Expected: FAIL — `cannot import name 'invoice_edit_modal'`.

- [ ] **Step 3: Implement `invoice_edit_modal`**

In `app/blocks.py`:

```python
def invoice_edit_modal(op_id: str, lines: list[dict], coa_options: list[tuple[str, str]]) -> dict:
    """Modal to correct each flagged line's account code / tax code / amount.

    ``coa_options`` is a list of (code, label) for the static_select; ``lines`` is
    the proposed extraction. ``block_id`` encodes the line index: ``acct_<i>`` etc.
    """
    coa = [{"text": {"type": "plain_text", "text": lbl[:75]}, "value": code}
           for code, lbl in coa_options]
    tax_opts = [{"text": {"type": "plain_text", "text": t}, "value": t}
                for t in ("SR", "ZR", "ES", "TX", "OS")]
    blocks: list = []
    for i, ln in enumerate(lines):
        blocks.append({"type": "section",
                       "text": {"type": "mrkdwn", "text": f"*Line {i + 1}: {ln.get('description', '')}*"}})
        acct_initial = next((o for o in coa if o["value"] == ln.get("account_code")), None)
        blocks.append({
            "type": "input", "block_id": f"acct_{i}", "optional": True,
            "label": {"type": "plain_text", "text": "Account code"},
            "element": {"type": "static_select", "action_id": "v", "options": coa,
                        **({"initial_option": acct_initial} if acct_initial else {})},
        })
        tax_initial = next((o for o in tax_opts if o["value"] == ln.get("tax_code")), None)
        blocks.append({
            "type": "input", "block_id": f"tax_{i}", "optional": True,
            "label": {"type": "plain_text", "text": "Tax code"},
            "element": {"type": "static_select", "action_id": "v", "options": tax_opts,
                        **({"initial_option": tax_initial} if tax_initial else {})},
        })
        blocks.append({
            "type": "input", "block_id": f"amt_{i}", "optional": True,
            "label": {"type": "plain_text", "text": "Amount"},
            "element": {"type": "number_input", "action_id": "v", "is_decimal_allowed": True,
                        **({"initial_value": str(ln["amount"])} if ln.get("amount") is not None else {})},
        })
    return {
        "type": "modal", "callback_id": "ledgr_invoice_edit", "private_metadata": op_id,
        "title": {"type": "plain_text", "text": "Review invoice"},
        "submit": {"type": "plain_text", "text": "Post to ledger"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": blocks,
    }
```

- [ ] **Step 4: Run it to verify it passes**

Run: `.venv/bin/pytest tests/test_app_blocks.py::test_invoice_edit_modal_prefills_and_carries_op_id -q`
Expected: PASS.

- [ ] **Step 5: Write the failing test for parsing a `view_submission` into edits**

Add to `tests/test_slack_runner.py`:

```python
def test_edits_from_view_state_builds_line_edits():
    from accounting_agents.slack_runner import _edits_from_view_state
    view = {"state": {"values": {
        "acct_0": {"v": {"selected_option": {"value": "6010"}}},
        "tax_0":  {"v": {"selected_option": {"value": "ZR"}}},
        "amt_0":  {"v": {"value": "44.74"}},
    }}}
    edits = _edits_from_view_state(view)
    assert edits == {"lines": [{"index": 0, "account_code": "6010",
                                "tax_code": "ZR", "amount": 44.74}]}
```

- [ ] **Step 6: Run it to verify it fails**

Run: `.venv/bin/pytest tests/test_slack_runner.py::test_edits_from_view_state_builds_line_edits -q`
Expected: FAIL — `cannot import name '_edits_from_view_state'`.

- [ ] **Step 7: Implement `_edits_from_view_state`**

In `accounting_agents/slack_runner.py`:

```python
def _edits_from_view_state(view: dict) -> dict:
    """Convert a ``view_submission`` state into the line-edits dict."""
    values = (view.get("state") or {}).get("values") or {}
    by_index: dict[int, dict] = {}
    for block_id, payload in values.items():
        prefix, _, idx_s = block_id.partition("_")
        if not idx_s.isdigit():
            continue
        i = int(idx_s)
        el = payload.get("v") or {}
        if prefix == "acct" and el.get("selected_option"):
            by_index.setdefault(i, {})["account_code"] = el["selected_option"]["value"]
        elif prefix == "tax" and el.get("selected_option"):
            by_index.setdefault(i, {})["tax_code"] = el["selected_option"]["value"]
        elif prefix == "amt" and el.get("value"):
            by_index.setdefault(i, {})["amount"] = float(el["value"])
    lines = [{"index": i, **fields} for i, fields in sorted(by_index.items())]
    return {"lines": lines}
```

- [ ] **Step 8: Run it to verify it passes**

Run: `.venv/bin/pytest tests/test_slack_runner.py::test_edits_from_view_state_builds_line_edits -q`
Expected: PASS.

- [ ] **Step 9: Rewire the `edit` action to open the modal + add the view handler**

In `accounting_agents/slack_runner.py` `build_async_app`, replace the `_edit` handler and add a view handler. The `edit` handler must open the modal synchronously (fresh `trigger_id`), reading the proposed lines from the paused session/interrupt:

```python
    @async_app.action("edit")
    async def _edit(ack, body, client):
        await ack()
        op_id = (body.get("actions") or [{}])[0].get("value")
        if not op_id:
            return
        interrupt = read_interrupt(db, op_id)
        state = await _read_session_state(runner, app_name, interrupt) if interrupt else {}
        invs = state.get("normalized_invoices") or [{}]
        lines = invs[0].get("lines") or []
        coa_options = [(c.get("code"), f"{c.get('code')} — {c.get('description')}")
                       for c in (state.get("coa") or [])]
        sync_client.views_open(
            trigger_id=body["trigger_id"],
            view=invoice_edit_modal(op_id, lines, coa_options),
        )

    @async_app.view("ledgr_invoice_edit")
    async def _edit_submit(ack, body, client):
        await ack()
        view = body["view"]
        op_id = view.get("private_metadata") or ""
        edits = _edits_from_view_state(view)
        await handle_approval_action(
            runner=runner, ledger_store=ledger_store, db=db, slack_client=sync_client,
            op_id=op_id, decision="edit", edits=edits, app_name=app_name,
        )
```

Add a small `_read_session_state(runner, app_name, interrupt)` helper that fetches the paused session (`user_id`/`session_id` from the interrupt doc) and returns `dict(session.state)` — mirror the session fetch already used in `persist_and_deliver` (`slack_runner.py:223-226`). Ensure `invoice_edit_modal` and `read_interrupt` are imported.

- [ ] **Step 10: Run the full Phase-3 suites**

Run: `.venv/bin/pytest tests/test_slack_runner.py tests/test_app_blocks.py tests/test_nodes.py -q`
Expected: PASS.

- [ ] **Step 11: Commit**

```bash
git add accounting_agents/slack_runner.py app/blocks.py tests/test_app_blocks.py tests/test_slack_runner.py
git commit -m "feat(hitl): Edit button opens a modal that resumes the workflow with line edits"
```

### Task 8: An edit becomes a per-client Correction

**Files:**
- Modify: `accounting_agents/nodes.py` (`apply_decision_node` — on `edit`, also record a Correction in state for the runner to persist) OR persist directly in the view handler. This task persists from the runner after a successful edit.
- Modify: `accounting_agents/slack_runner.py` (after a successful `edit` resume, write the Correction via the client store)
- Test: `tests/test_slack_runner.py`

> A Correction (ADR-0004 / CONTEXT.md) maps a vendor → account/tax code so the next document auto-applies it. Persist one per edited line that carries an account or tax change, keyed by the invoice's vendor.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_slack_runner.py`:

```python
def test_persist_corrections_writes_vendor_mapping():
    from accounting_agents.slack_runner import _persist_corrections

    saved = []

    class _Store:
        def add_correction(self, *, client_id, vendor, account_code=None, tax_code=None):
            saved.append((client_id, vendor, account_code, tax_code))

    state = {"client_id": "CL-1", "normalized_invoices": [
        {"vendor_name": "Hotel Booking",
         "lines": [{"description": "Room", "account_code": "6010", "tax_code": "ZR"}]}
    ]}
    edits = {"lines": [{"index": 0, "account_code": "6010", "tax_code": "ZR"}]}
    _persist_corrections(_Store(), state, edits)
    assert saved == [("CL-1", "Hotel Booking", "6010", "ZR")]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/pytest tests/test_slack_runner.py::test_persist_corrections_writes_vendor_mapping -q`
Expected: FAIL — `cannot import name '_persist_corrections'`.

- [ ] **Step 3: Confirm / add the store method**

Check `invoice_processing/export/client_context.py` for an `add_correction` (or entity-memory upsert) on the store. If absent, add to `FirestoreClientStore` (and the in-memory store used by tests):

```python
    def add_correction(self, *, client_id: str, vendor: str,
                       account_code: str | None = None, tax_code: str | None = None) -> None:
        """Upsert a per-client vendor→code Correction (entity memory)."""
        if not (client_id and vendor):
            return
        db = self._firestore()
        ref = db.collection(self._collection).document(client_id) \
                .collection("entity_memory").document(vendor)
        patch = {"name": vendor}
        if account_code: patch["mapping_code"] = account_code
        if tax_code: patch["tax_code"] = tax_code
        ref.set(patch, merge=True)
```

(If an equivalent learning hook already exists, call that instead and adjust the test to its signature.)

- [ ] **Step 4: Implement `_persist_corrections`**

In `accounting_agents/slack_runner.py`:

```python
def _persist_corrections(client_store, state: dict, edits: dict) -> None:
    """Persist each account/tax edit as a per-client vendor Correction (ADR-0004)."""
    client_id = state.get("client_id")
    invs = state.get("normalized_invoices") or []
    if not client_id or not invs:
        return
    vendor = invs[0].get("vendor_name") or invs[0].get("issuer_name")
    for e in (edits.get("lines") or []):
        if e.get("account_code") or e.get("tax_code"):
            client_store.add_correction(
                client_id=client_id, vendor=vendor,
                account_code=e.get("account_code"), tax_code=e.get("tax_code"),
            )
```

- [ ] **Step 5: Call it from the view-submission handler after a successful edit**

In the `@async_app.view("ledgr_invoice_edit")` handler (Task 7), after `handle_approval_action(...)`, read the (now-updated) session state and persist:

```python
        state = await _read_session_state(runner, app_name, read_interrupt(db, op_id))
        _persist_corrections(_DEFAULT_CLIENT_STORE, state, edits)
```

- [ ] **Step 6: Run it to verify it passes**

Run: `.venv/bin/pytest tests/test_slack_runner.py -k corrections -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add accounting_agents/slack_runner.py invoice_processing/export/client_context.py tests/test_slack_runner.py
git commit -m "feat(hitl): persist approve-with-edit as a per-client Correction (ADR-0004)"
```

### Task 9: One Job summary for a batch drop (replace per-doc spam)

**Files:**
- Modify: `app/slack_app.py` (the multi-file upload handler that iterates `documents` and calls `process_file_event` per file) — post one Job summary, run docs, edit it with the tally
- Modify: `app/blocks.py` (new `job_summary_text(total, posted, needs_review)`)
- Test: `tests/test_app_blocks.py`, `tests/test_slack_app.py`

- [ ] **Step 1: Write the failing summary-text test**

Add to `tests/test_app_blocks.py`:

```python
from app.blocks import job_summary_text


def test_job_summary_text_counts():
    t = job_summary_text(total=10, posted=7, needs_review=3, software="Xero", fy="2026")
    assert "10" in t and "7" in t and "3" in t and "Xero" in t
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/pytest tests/test_app_blocks.py::test_job_summary_text_counts -q`
Expected: FAIL — `cannot import name 'job_summary_text'`.

- [ ] **Step 3: Implement `job_summary_text`**

In `app/blocks.py`:

```python
def job_summary_text(*, total: int, posted: int, needs_review: int,
                     software: str = "", fy: str = "") -> str:
    """One-line Job summary for a batch drop ([[Batch (Job)]])."""
    tgt = f" {software}" if software else ""
    fyl = f" FY{fy}" if fy else ""
    head = f"📥 Processed {total} document{'s' if total != 1 else ''}"
    body = f" — {posted} posted to your{tgt}{fyl} ledger"
    tail = f", {needs_review} need your review" if needs_review else ""
    return head + body + tail
```

- [ ] **Step 4: Run it to verify it passes**

Run: `.venv/bin/pytest tests/test_app_blocks.py::test_job_summary_text_counts -q`
Expected: PASS.

- [ ] **Step 5: Write the failing batch-handler test**

In `tests/test_slack_app.py` (mirror the existing multi-file handler test), assert that dropping 3 files posts exactly ONE job-summary message (not 3 separate status messages) and that each per-doc card is threaded under it. Use the existing fakes; assert on the count of top-level `chat_postMessage` calls without `thread_ts` vs. those with `thread_ts`.

```python
def test_batch_drop_posts_one_job_summary(monkeypatch):
    # ... build fake event with 3 files, fake client recording posts ...
    # process, then:
    top_level = [c for c in client.posts if not c.get("thread_ts")]
    threaded = [c for c in client.posts if c.get("thread_ts")]
    assert len(top_level) == 1            # single Job summary
    assert len(threaded) >= 1             # per-doc cards in the thread
```

(Flesh out with the file-event fixture the existing tests use; if the current handler posts per-file status messages, this test will fail until Step 7.)

- [ ] **Step 6: Run it to verify it fails**

Run: `.venv/bin/pytest tests/test_slack_app.py -k job_summary -q`
Expected: FAIL — multiple top-level messages today.

- [ ] **Step 7: Implement — post one summary, thread the per-doc work under it**

In `app/slack_app.py`, in the handler that iterates dropped `documents`: before the loop, post the Job summary message and capture its `ts`; pass that `ts` as `thread_ts` into `process_file_event` (add a `thread_ts` param threaded through to the status + card posts in `slack_runner.py`); after the loop, `chat_update` the summary message with the final `job_summary_text(...)` tally. Keep per-doc sessions unchanged — only the presentation aggregates.

- [ ] **Step 8: Run the app + runner suites**

Run: `.venv/bin/pytest tests/test_slack_app.py tests/test_slack_runner.py tests/test_app_blocks.py -q`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add app/slack_app.py app/blocks.py accounting_agents/slack_runner.py tests/test_slack_app.py tests/test_app_blocks.py
git commit -m "feat(hitl): collapse a batch drop into one threaded Job summary (ADR-0007)"
```

---

## Final verification

- [ ] **Run the whole suite:** `.venv/bin/pytest -q` — expected: all green.
- [ ] **Lint:** `ruff check accounting_agents app invoice_processing` — expected: clean.
- [ ] **Live smoke (manual):** restart the socket bot (see memory `local-bot-run-and-live-state`), in a Xero-configured channel drop one flagged invoice → confirm: profile card on `/ledgr profile`; delivery says "your *Xero* … ledger"; review card names the file; **Edit** opens a modal; submitting writes the corrected line; re-dropping the same vendor auto-applies the Correction.
- [ ] **Firestore housekeeping (separate, optional):** delete the 7 orphan all-null client docs surfaced during diagnosis.

---

## Self-review notes

- **Spec coverage:** Problem 1 (wrong template) → Phase 1 (Tasks 1–2). Problem 3 (no signup confirmation) → Phase 2 (Tasks 3–4). Problem 2 (Edit no-op / can't identify doc / 10× spam) → Phase 3 (Tasks 5–9). Correction-memory → Task 8.
- **Type consistency:** `edits = {"lines": [{"index", "account_code", "tax_code", "amount", "description"}]}` is produced by `_edits_from_view_state` (Task 7), consumed by `apply_decision_node` (Task 6) and `_persist_corrections` (Task 8) with matching keys. `software` key flows `to_state()` → `state_delta` → `consolidate_node` payload → `deliver_node` echo. `op_id` is the button `value` and the modal `private_metadata`.
- **Known seam to verify during execution:** Task 7's `_read_session_state` mirrors the session fetch in `persist_and_deliver`; confirm the paused session's `state` contains `normalized_invoices` and `coa` at pause time (seeded by Phase 1). If `coa` is empty for a client, the account-code select renders empty — acceptable (free-typed amount/tax still work), but worth a follow-up to require COA before edit.
