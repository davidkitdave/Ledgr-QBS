"""Tests for Lever 4: per-client familiarity gate (ADR-0017 §6).

Hermetic — InMemoryClientStore + injected FakeFirestore; no live GCP, no Gemini, no Slack.

Covers:
- record_familiarity increments seen_count; reset_familiarity zeroes it.
- Both key granularities: doc_type-only and doc_type:vendor compound key.
- detect_struggle (most-specific-key gating):
    * vendor present → compound key governs (bare key irrelevant).
    * no vendor → bare doc_type key governs.
    * soft-only + compound key seen_count >= 2 → (False, []) suppressed.
    * soft-only + bare key seen_count >= 2 (no vendor) → (False, []) suppressed.
    * seen_count < 2 → still trips (critic path).
    * hard signal + high familiarity → STILL trips.
- 4c reset: after correction resets compound key, that vendor's soft-only doc
  trips again — the surviving bare key does NOT rescue it.
- Firestore record_familiarity: injected fake client; patch carries firestore.Increment.
- _profile_state_delta bridge: familiarity map lands in state delta.
- Real handle_approval_action hook: approve records familiarity; edit does NOT.
"""

from __future__ import annotations

import asyncio
import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from accounting_agents import nodes, slack_runner
from accounting_agents.nodes import detect_struggle
from invoice_processing.export.client_context import (
    ClientContext,
    FirestoreClientStore,
    InMemoryClientStore,
)
from invoice_processing.export.models import InvoiceLine, NormalizedInvoice, PartyInfo
from tests._fake_firestore import FakeFirestore


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _make_store(client_id: str = "client-1") -> InMemoryClientStore:
    store = InMemoryClientStore()
    ctx = ClientContext(client_id=client_id)
    store.add(ctx)
    return store


def _clean_invoice(vendor: str | None = "Acme Corp", **overrides) -> NormalizedInvoice:
    """Build a clean reconciled invoice.  Pass vendor=None for a vendorless invoice."""
    defaults: dict = dict(
        invoice_number="INV-1",
        invoice_date=datetime.date(2025, 1, 15),
        doc_total=109.0,
        reconciled=True,
        our_gst_registered=True,
        lines=[InvoiceLine(description="Goods", net_amount=100.0, gst_amount=9.0)],
    )
    if vendor is not None:
        defaults["supplier"] = PartyInfo(name=vendor)
    defaults.update(overrides)
    return NormalizedInvoice(**defaults)


def _state(invoices=None, *, doc_type="other", confidence=0.6,
           familiarity: dict | None = None, **extra) -> dict:
    """Build a minimal state dict for detect_struggle."""
    inv_dicts = [nodes._inv_to_dict(i) for i in (invoices or [])]
    state: dict = {
        nodes.NORMALIZED_KEY: inv_dicts,
        nodes.DOC_TYPE_KEY: doc_type,
        nodes.CLASSIFY_CONFIDENCE_KEY: confidence,
    }
    if familiarity is not None:
        state[nodes.FAMILIARITY_KEY] = familiarity
    state.update(extra)
    return state


def _soft_state(vendor: str | None = "Acme Corp",
                doc_type: str = "other",
                fam: dict | None = None) -> dict:
    """State that will trip SOFT-only signals with given familiarity map.

    confidence=0.1 triggers low_classify_confidence (a soft signal).
    If vendor is None, no supplier is set on the invoice (vendorless path).
    """
    inv = _clean_invoice(vendor=vendor)
    return _state(
        invoices=[inv],
        doc_type=doc_type,
        confidence=0.1,
        familiarity=fam if fam is not None else {},
    )


# --------------------------------------------------------------------------- #
# Part A — InMemoryClientStore.record_familiarity / reset_familiarity
# --------------------------------------------------------------------------- #


class TestInMemoryFamiliarityStore:
    def test_record_increments_doc_type_key(self):
        store = _make_store()
        store.record_familiarity(client_id="client-1", doc_type="expense_claim")
        assert store.get_familiarity("client-1", "expense_claim") == 1
        store.record_familiarity(client_id="client-1", doc_type="expense_claim")
        assert store.get_familiarity("client-1", "expense_claim") == 2

    def test_record_increments_vendor_compound_key(self):
        store = _make_store()
        store.record_familiarity(client_id="client-1", doc_type="invoice",
                                 vendor="Acme Corp")
        assert store.get_familiarity("client-1", "invoice", vendor="Acme Corp") == 1
        store.record_familiarity(client_id="client-1", doc_type="invoice",
                                 vendor="Acme Corp")
        assert store.get_familiarity("client-1", "invoice", vendor="Acme Corp") == 2

    def test_record_both_keys_simultaneously(self):
        """record_familiarity with vendor increments BOTH doc_type and doc_type:vendor."""
        store = _make_store()
        store.record_familiarity(client_id="client-1", doc_type="invoice",
                                 vendor="Acme Corp")
        assert store.get_familiarity("client-1", "invoice") == 1
        assert store.get_familiarity("client-1", "invoice", vendor="Acme Corp") == 1

    def test_reset_zeroes_doc_type_key(self):
        store = _make_store()
        store.record_familiarity(client_id="client-1", doc_type="invoice")
        store.record_familiarity(client_id="client-1", doc_type="invoice")
        assert store.get_familiarity("client-1", "invoice") == 2
        store.reset_familiarity(client_id="client-1", doc_type="invoice")
        assert store.get_familiarity("client-1", "invoice") == 0

    def test_reset_zeroes_vendor_compound_key(self):
        store = _make_store()
        store.record_familiarity(client_id="client-1", doc_type="invoice",
                                 vendor="Acme Corp")
        store.record_familiarity(client_id="client-1", doc_type="invoice",
                                 vendor="Acme Corp")
        assert store.get_familiarity("client-1", "invoice", vendor="Acme Corp") == 2
        store.reset_familiarity(client_id="client-1", doc_type="invoice",
                                vendor="Acme Corp")
        assert store.get_familiarity("client-1", "invoice", vendor="Acme Corp") == 0

    def test_reset_with_vendor_resets_compound_key_not_dt_key(self):
        """reset_familiarity(vendor=X) resets only the compound key, not bare doc_type."""
        store = _make_store()
        store.record_familiarity(client_id="client-1", doc_type="invoice")
        store.record_familiarity(client_id="client-1", doc_type="invoice",
                                 vendor="Acme Corp")
        store.reset_familiarity(client_id="client-1", doc_type="invoice",
                                vendor="Acme Corp")
        assert store.get_familiarity("client-1", "invoice", vendor="Acme Corp") == 0
        # bare key was incremented twice (once bare, once with vendor) → still 2
        assert store.get_familiarity("client-1", "invoice") == 2

    def test_noop_for_missing_client(self):
        store = _make_store()
        store.record_familiarity(client_id="no-such-client", doc_type="invoice")
        assert store.get_familiarity("no-such-client", "invoice") == 0

    def test_noop_for_empty_doc_type(self):
        store = _make_store()
        store.record_familiarity(client_id="client-1", doc_type="")
        # Must not crash; nothing recorded


# --------------------------------------------------------------------------- #
# Part B — detect_struggle most-specific-key gating
# --------------------------------------------------------------------------- #


class TestDetectStruggleFamiliarityGate:
    # ------------------------------------------------------------------
    # Vendor-identifiable path — compound key governs
    # ------------------------------------------------------------------

    def test_vendor_known_compound_key_below_threshold_trips(self):
        """Vendor known, compound key count=1 < 2 → soft signals still trip."""
        state = _soft_state(
            vendor="Acme Corp", doc_type="other",
            fam={"other:Acme Corp": {"seen_count": 1}},
        )
        tripped, reasons = detect_struggle(state)
        assert tripped is True
        assert reasons

    def test_vendor_known_compound_key_at_threshold_suppresses(self):
        """Vendor known, compound key count=2 → suppress soft signals."""
        state = _soft_state(
            vendor="Acme Corp", doc_type="other",
            fam={"other:Acme Corp": {"seen_count": 2}},
        )
        tripped, reasons = detect_struggle(state)
        assert tripped is False
        assert reasons == []

    def test_vendor_known_compound_key_above_threshold_suppresses(self):
        """Vendor known, compound key count=5 → suppress."""
        state = _soft_state(
            vendor="Acme Corp", doc_type="other",
            fam={"other:Acme Corp": {"seen_count": 5}},
        )
        tripped, reasons = detect_struggle(state)
        assert tripped is False
        assert reasons == []

    def test_vendor_known_bare_key_only_does_not_suppress(self):
        """Vendor known, bare key at threshold but compound key absent → NOT suppressed.

        This is the 4c fix: a vendor correction zeros the compound key; the
        surviving bare key must NOT rescue the corrected vendor's next doc.
        """
        state = _soft_state(
            vendor="Acme Corp", doc_type="other",
            fam={"other": {"seen_count": 99}},  # bare key high, compound absent
        )
        tripped, reasons = detect_struggle(state)
        assert tripped is True
        assert reasons

    # ------------------------------------------------------------------
    # No-vendor path — bare doc_type key governs
    # ------------------------------------------------------------------

    def test_no_vendor_bare_key_below_threshold_trips(self):
        """No vendor on invoice, bare key count=1 < 2 → still trips."""
        state = _soft_state(
            vendor=None, doc_type="other",
            fam={"other": {"seen_count": 1}},
        )
        tripped, reasons = detect_struggle(state)
        assert tripped is True

    def test_no_vendor_bare_key_at_threshold_suppresses(self):
        """No vendor on invoice, bare key count=2 → suppress (vendorless fallback)."""
        state = _soft_state(
            vendor=None, doc_type="other",
            fam={"other": {"seen_count": 2}},
        )
        tripped, reasons = detect_struggle(state)
        assert tripped is False
        assert reasons == []

    # ------------------------------------------------------------------
    # Hard-signal safety
    # ------------------------------------------------------------------

    def test_hard_signal_with_high_familiarity_still_trips(self):
        """Hard signal (unreconciled) → never suppressed, even at seen_count=99."""
        inv = _clean_invoice(vendor="Acme Corp")
        inv.reconciled = False
        inv.reconcile_note = "totals do not reconcile"
        state = _state(
            invoices=[inv],
            doc_type="other",
            confidence=0.9,
            familiarity={"other:Acme Corp": {"seen_count": 99}},
        )
        tripped, reasons = detect_struggle(state)
        assert tripped is True
        assert any("unreconciled" in r for r in reasons)

    def test_mixed_soft_and_hard_not_suppressed(self):
        """Hard signal present → _is_soft_only False → familiarity gate bypassed."""
        inv = _clean_invoice()
        inv.lines = []  # lines_empty is hard
        state = _state(
            invoices=[inv],
            doc_type="other",
            confidence=0.1,
            familiarity={"other:Acme Corp": {"seen_count": 99}},
        )
        tripped, reasons = detect_struggle(state)
        assert tripped is True

    # ------------------------------------------------------------------
    # No familiarity / empty map
    # ------------------------------------------------------------------

    def test_no_familiarity_key_in_state_behaves_as_zero(self):
        """FAMILIARITY_KEY absent → no suppression."""
        inv = _clean_invoice()
        state = _state(invoices=[inv], doc_type="other", confidence=0.1)
        # familiarity key deliberately absent
        tripped, _ = detect_struggle(state)
        assert tripped is True

    def test_empty_familiarity_dict_behaves_as_zero(self):
        """Empty familiarity dict → no suppression."""
        state = _soft_state(vendor="Acme Corp", doc_type="other", fam={})
        tripped, _ = detect_struggle(state)
        assert tripped is True


# --------------------------------------------------------------------------- #
# Part C — 4c reset: vendor correction breaks suppression for that vendor
# --------------------------------------------------------------------------- #


class TestResetOnCorrection:
    def test_correction_resets_compound_key_and_detect_struggle_trips_again(self):
        """After correction resets the compound key, the vendor's next soft-only
        doc trips detect_struggle (bare key does NOT rescue it).
        """
        store = _make_store()
        # Bring compound key to threshold
        store.record_familiarity(client_id="client-1", doc_type="other",
                                 vendor="Acme Corp")
        store.record_familiarity(client_id="client-1", doc_type="other",
                                 vendor="Acme Corp")
        assert store.get_familiarity("client-1", "other", vendor="Acme Corp") == 2

        # Correction lands → only compound key is reset
        store.reset_familiarity(client_id="client-1", doc_type="other",
                                vendor="Acme Corp")
        assert store.get_familiarity("client-1", "other", vendor="Acme Corp") == 0
        # bare key is still 2 (record_familiarity dual-wrote it)
        assert store.get_familiarity("client-1", "other") == 2

        # Build state from the post-correction familiarity map.
        # Vendor is identifiable → gate checks compound key only → compound=0 < 2
        # → NOT suppressed, even though bare key is 2.
        fam = store.get_familiarity_map("client-1")
        state = _soft_state(vendor="Acme Corp", doc_type="other", fam=fam)
        tripped, reasons = detect_struggle(state)
        assert tripped is True, (
            "After correction the vendor's compound key is 0; "
            "the surviving bare key must NOT rescue suppression."
        )
        assert reasons

    def test_dt_key_not_reset_when_only_vendor_correction(self):
        """Store-level: reset_familiarity(vendor=X) leaves the bare doc_type key intact."""
        store = _make_store()
        store.record_familiarity(client_id="client-1", doc_type="other",
                                 vendor="Acme Corp")
        store.record_familiarity(client_id="client-1", doc_type="other",
                                 vendor="Acme Corp")
        store.reset_familiarity(client_id="client-1", doc_type="other",
                                vendor="Acme Corp")
        # compound zeroed
        assert store.get_familiarity("client-1", "other", vendor="Acme Corp") == 0
        # bare key untouched (dual-written twice → 2)
        assert store.get_familiarity("client-1", "other") == 2

    def test_other_vendor_unaffected_by_correction(self):
        """Correction for vendor A does not affect vendor B's familiarity."""
        store = _make_store()
        store.record_familiarity(client_id="client-1", doc_type="other",
                                 vendor="Vendor A")
        store.record_familiarity(client_id="client-1", doc_type="other",
                                 vendor="Vendor A")
        store.record_familiarity(client_id="client-1", doc_type="other",
                                 vendor="Vendor B")
        store.record_familiarity(client_id="client-1", doc_type="other",
                                 vendor="Vendor B")
        # Correct Vendor A only
        store.reset_familiarity(client_id="client-1", doc_type="other",
                                vendor="Vendor A")
        assert store.get_familiarity("client-1", "other", vendor="Vendor A") == 0
        # Vendor B compound key intact
        assert store.get_familiarity("client-1", "other", vendor="Vendor B") == 2


# --------------------------------------------------------------------------- #
# Part D — Firestore record_familiarity: patch carries a real Increment transform
# --------------------------------------------------------------------------- #


class TestFirestoreRecordFamiliarityPatch:
    """Verify FirestoreClientStore.record_familiarity writes a real firestore.Increment
    transform (not a plain int).  Uses the injected-client seam (_injected_client)
    so no live GCP is touched.  This is the regression guard for the blocking bug
    where ``from google.cloud.firestore import INCREMENT`` silently failed.
    """

    def _make_firestore_store(self) -> tuple[FirestoreClientStore, FakeFirestore]:
        db = FakeFirestore()
        store = FirestoreClientStore(collection="clients", client=db)
        return store, db

    def test_record_familiarity_patch_carries_increment_transform(self):
        """The patch written to Firestore must contain a firestore.Increment object,
        not a plain integer — otherwise the atomic server-side increment never fires
        and seen_count silently stays at 0 in production.
        """
        from google.cloud import firestore as _fs

        store, db = self._make_firestore_store()
        store.record_familiarity(
            client_id="client-abc",
            doc_type="expense_claim",
            vendor="Acme Corp",
            direction="purchase",
        )

        # Both the bare and compound keys should have been written.
        bare_doc = (
            db.collection("clients")
            .document("client-abc")
            .collection("familiarity")
            .document("expense_claim")
            .get()
        )
        assert bare_doc.exists, "bare doc_type key not written"
        bare_data = bare_doc.to_dict()
        assert "seen_count" in bare_data, "seen_count field missing"
        assert isinstance(bare_data["seen_count"], _fs.Increment), (
            f"seen_count must be a firestore.Increment transform, got {type(bare_data['seen_count'])}"
        )

        compound_doc = (
            db.collection("clients")
            .document("client-abc")
            .collection("familiarity")
            .document("expense_claim:Acme Corp")
            .get()
        )
        assert compound_doc.exists, "compound doc_type:vendor key not written"
        compound_data = compound_doc.to_dict()
        assert isinstance(compound_data["seen_count"], _fs.Increment), (
            "compound seen_count must also be a firestore.Increment transform"
        )

    def test_record_familiarity_without_vendor_writes_only_bare_key(self):
        """When vendor is absent, only the bare doc_type key is written."""
        from google.cloud import firestore as _fs

        store, db = self._make_firestore_store()
        store.record_familiarity(client_id="client-abc", doc_type="invoice")

        bare_doc = (
            db.collection("clients")
            .document("client-abc")
            .collection("familiarity")
            .document("invoice")
            .get()
        )
        assert bare_doc.exists
        assert isinstance(bare_doc.to_dict()["seen_count"], _fs.Increment)

        # Compound key must NOT have been written
        compound_doc = (
            db.collection("clients")
            .document("client-abc")
            .collection("familiarity")
            .document("invoice:None")
            .get()
        )
        assert not compound_doc.exists, "no compound key should be written when vendor is absent"


# --------------------------------------------------------------------------- #
# Part E — _profile_state_delta bridge: familiarity map in state delta
# --------------------------------------------------------------------------- #


class TestProfileStateDeltaBridge:
    """_profile_state_delta must inject nodes.FAMILIARITY_KEY into the state delta
    so that detect_struggle can read it at run start without touching the store.
    """

    def test_familiarity_map_present_in_state_delta(self):
        store = _make_store(client_id="cl-1")
        # Register the client under a channel so get_by_channel resolves it.
        store.set_channel("C-TEST", "cl-1")

        # Record familiarity so the map is non-empty.
        store.record_familiarity(client_id="cl-1", doc_type="invoice",
                                 vendor="Acme Corp")
        store.record_familiarity(client_id="cl-1", doc_type="invoice",
                                 vendor="Acme Corp")

        delta = slack_runner._profile_state_delta(store, "C-TEST")

        assert nodes.FAMILIARITY_KEY in delta, (
            "_profile_state_delta must inject FAMILIARITY_KEY into the delta"
        )
        fam = delta[nodes.FAMILIARITY_KEY]
        assert "invoice" in fam
        assert fam["invoice"]["seen_count"] == 2
        assert "invoice:Acme Corp" in fam
        assert fam["invoice:Acme Corp"]["seen_count"] == 2

    def test_familiarity_key_present_even_when_empty(self):
        """Even a client with no recorded familiarity gets an empty dict in the delta
        (not KeyError — detect_struggle does state.get(FAMILIARITY_KEY) or {}).
        """
        store = _make_store(client_id="cl-2")
        store.set_channel("C-EMPTY", "cl-2")

        delta = slack_runner._profile_state_delta(store, "C-EMPTY")

        # Key must exist; value must be a dict (empty is fine).
        assert nodes.FAMILIARITY_KEY in delta
        assert isinstance(delta[nodes.FAMILIARITY_KEY], dict)

    def test_unknown_channel_returns_empty_delta(self):
        """A channel with no registered client → empty dict (no familiarity key)."""
        store = _make_store()
        delta = slack_runner._profile_state_delta(store, "C-UNKNOWN")
        assert delta == {}


# --------------------------------------------------------------------------- #
# Part F — Real handle_approval_action hook: approve records; edit does NOT
# --------------------------------------------------------------------------- #


class TestApprovalActionFamiliarityHook:
    """Drive the real handle_approval_action function with a monkeypatched
    _DEFAULT_CLIENT_STORE and assert that record_familiarity is called only
    on 'approve', not on 'edit'.

    Uses the same patterns established in test_slack_runner.py:
    - FakeFirestore for the interrupt/processed collections
    - AsyncMock for runner + persist_and_deliver
    - monkeypatch on slack_runner._DEFAULT_CLIENT_STORE
    - patch on slack_runner._read_session_state to return a seeded state
    """

    def _make_hitl_db(self) -> FakeFirestore:
        from accounting_agents.hitl import write_interrupt
        db = FakeFirestore()
        write_interrupt(
            db, "OP-FAM-1",
            session_id="S-FAM-1",
            channel_id="C-FAM-1",
            user_id="C-FAM-1",
            slack_file_id="F-FAM-1",
            message_ts="1.0",
        )
        return db

    def _fake_session_state(self) -> dict:
        """A minimal session state that _record_familiarity_from_state can read."""
        return {
            "client_id": "cl-fam",
            nodes.DOC_TYPE_KEY: "expense_claim",
            nodes.DIRECTION_KEY: "purchase",
            nodes.NORMALIZED_KEY: [
                {
                    "doc_type": "purchase",
                    "supplier": {"name": "Acme Corp"},
                    "customer": {"name": None},
                    "lines": [],
                }
            ],
        }

    def _run(self, decision: str, monkeypatch) -> tuple[list, list]:
        """Run handle_approval_action for the given decision.

        Returns (record_calls, reset_calls) recorded by the spy store.
        """
        from accounting_agents.slack_runner import handle_approval_action  # noqa: PLC0415

        record_calls: list = []
        reset_calls: list = []

        class _SpyStore:
            def add_correction(self, **_kwargs):
                pass

            def record_familiarity(self, *, client_id, doc_type,
                                   vendor=None, direction=None):
                record_calls.append(
                    {"client_id": client_id, "doc_type": doc_type,
                     "vendor": vendor, "direction": direction}
                )

            def reset_familiarity(self, *, client_id, doc_type, vendor=None):
                reset_calls.append(
                    {"client_id": client_id, "doc_type": doc_type, "vendor": vendor}
                )

            def append_processing_log(self, **_kwargs):
                pass

        monkeypatch.setattr(slack_runner, "_DEFAULT_CLIENT_STORE", _SpyStore())

        db = self._make_hitl_db()
        fake_state = self._fake_session_state()

        fake_runner = MagicMock()
        fake_runner.session_service.get_session = AsyncMock(
            return_value=SimpleNamespace(state=fake_state)
        )

        async def _fake_resume(*_a, **_kw):
            return []

        async def _fake_pad(*_a, **_kw):
            return {"appended": 1, "all_deduped": False}

        async def _fake_read_state(*_a, **_kw):
            return fake_state

        with (
            patch.object(slack_runner, "resume_session", _fake_resume),
            patch.object(slack_runner, "persist_and_deliver", _fake_pad),
            patch.object(slack_runner, "_read_session_state", _fake_read_state),
            patch.object(slack_runner, "_update_card", lambda *_a, **_kw: None),
            patch.object(slack_runner, "_post_message", lambda *_a, **_kw: None),
            patch.object(slack_runner, "update_interrupt_status", lambda *_a, **_kw: None),
            patch.object(slack_runner, "_update_status", lambda *_a, **_kw: None),
        ):
            asyncio.run(handle_approval_action(
                runner=fake_runner,
                ledger_store=MagicMock(),
                db=db,
                slack_client=MagicMock(),
                op_id="OP-FAM-1",
                decision=decision,
                app_name="test_app",
            ))

        return record_calls, reset_calls

    def test_approve_records_familiarity(self, monkeypatch):
        """decision='approve' → record_familiarity called with correct doc_type/vendor."""
        record_calls, _ = self._run("approve", monkeypatch)
        assert len(record_calls) == 1, (
            f"Expected 1 record_familiarity call on approve, got {len(record_calls)}"
        )
        call = record_calls[0]
        assert call["client_id"] == "cl-fam"
        assert call["doc_type"] == "expense_claim"
        assert call["vendor"] == "Acme Corp"

    def test_edit_does_not_record_familiarity(self, monkeypatch):
        """decision='edit' → record_familiarity must NOT be called."""
        record_calls, _ = self._run("edit", monkeypatch)
        assert record_calls == [], (
            f"record_familiarity must not fire on 'edit', got calls: {record_calls}"
        )

    def test_reject_does_not_record_familiarity(self, monkeypatch):
        """decision='reject' → record_familiarity must NOT be called."""
        record_calls, _ = self._run("reject", monkeypatch)
        assert record_calls == []


# --------------------------------------------------------------------------- #
# Part G — get_familiarity_map format
# --------------------------------------------------------------------------- #


class TestFamiliarityMapFormat:
    def test_map_structure_matches_expected_state_format(self):
        """get_familiarity_map returns {key: {seen_count: n}} shape."""
        store = _make_store()
        store.record_familiarity(client_id="client-1", doc_type="invoice",
                                 vendor="Acme Corp")
        store.record_familiarity(client_id="client-1", doc_type="invoice",
                                 vendor="Acme Corp")
        fam = store.get_familiarity_map("client-1")

        assert "invoice" in fam
        assert fam["invoice"]["seen_count"] == 2
        assert "invoice:Acme Corp" in fam
        assert fam["invoice:Acme Corp"]["seen_count"] == 2

    def test_map_empty_for_unknown_client(self):
        store = _make_store()
        fam = store.get_familiarity_map("no-such-client")
        assert fam == {}
