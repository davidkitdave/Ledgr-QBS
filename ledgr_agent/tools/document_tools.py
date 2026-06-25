from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import os
import time
from typing import Any

from invoice_processing.pipeline import BatchResult as EngineBatchResult
from invoice_processing.pipeline import process_batch as engine_process_batch
from invoice_processing.shared_libraries.model_config import lite_model, resolve_model
from invoice_processing.export.client_context import client_context_from_state
from invoice_processing.extract.invoice_extractor import mime_for
from invoice_processing.extract.segmentation_gates import count_input_pages
from ledgr_agent.schemas.credit import CreditSummary
from ledgr_agent.policies import load_jurisdiction_policy
from ledgr_agent.policies.constants import HARD_VIOLATION_IDS  # noqa: F401 — shared contract; IDs emitted here belong to this set
from ledgr_agent.policies.validators import validate_gst_registration_gate
from ledgr_agent.tools.batch_mapper import map_engine_batch_to_contract
from ledgr_agent.tools.document_engine import process_batch_with_document_spine
from ledgr_agent.tools.document_truth import document_truth_report
from ledgr_agent.tools.playground_uploads import resolve_document_paths

PipelineInject = dict[str, Callable[..., Any]]


# ---------------------------------------------------------------------------
# Credit service singleton — process-persistent in dev/eval
# ---------------------------------------------------------------------------
# The factory seam is checked first so tests can monkeypatch
# ``_credit_service_factory`` to inject a hermetic store.  When no factory is
# registered we lazily create ONE ``CreditService(InMemoryCreditStore())`` for
# the lifetime of the process (``_credit_service_singleton``).  This fixes the
# previous bug where every call built a fresh empty store, making
# ``read_balance`` always return 0.
#
# PRODUCTION NOTE: a Cloud-Run startup hook (e.g. in app/main.py or
# slack_runner.py) registers ``_credit_service_factory`` pointing at a
# Firestore-backed ``CreditStore`` implementation.  Do NOT implement Firestore
# here — this module stays storage-agnostic.
_credit_service_factory: Callable[[], Any] | None = None
_credit_service_singleton: Any = None  # lazily initialised below


def _get_credit_service() -> Any:
    """Return a :class:`CreditService` for the current process.

    Priority order:
    1. ``_credit_service_factory()`` — registered by production startup or tests
       (monkeypatch ``ledgr_agent.tools.document_tools._credit_service_factory``).
    2. Module-level singleton backed by ``InMemoryCreditStore`` — created once
       and reused for the rest of the process lifetime so balances persist
       across calls in dev/eval runs.
    """
    global _credit_service_singleton

    if _credit_service_factory is not None:
        return _credit_service_factory()

    if _credit_service_singleton is not None:
        return _credit_service_singleton

    try:
        from app.credit_service import CreditService, InMemoryCreditStore
    except ImportError:
        return None

    _credit_service_singleton = CreditService(InMemoryCreditStore())
    return _credit_service_singleton


def _credit_gate(
    *,
    firm_id: str | None,
    paths: list[str],
    required_units: int | None = None,
) -> dict[str, Any]:
    """Pre-engine credit gate.

    Returns ``{"allowed": bool, "reason": str, "balance": int}``. When the credit
    service is unavailable (e.g. the test environment imports ``document_tools``
    without the ``app`` package on the path) the gate is a no-op allow.
    Production wires a Firestore-backed implementation by registering a
    ``_credit_service_factory`` (or monkeypatching it in tests).
    """
    if not firm_id:
        return {"allowed": True, "reason": "ok", "balance": 0}

    service = _get_credit_service()
    if service is None:
        return {"allowed": True, "reason": "ok", "balance": 0}

    required = max(required_units if required_units is not None else len(paths), 0)
    balance = service.read_balance(firm_id)
    allowed = balance >= required
    if allowed:
        reason = "ok"
    elif balance <= 0:
        reason = "zero_credit"
    else:
        reason = "insufficient_credit"
    return {
        "allowed": allowed,
        "reason": reason,
        "balance": balance,
        "required_units": required,
    }


def _estimate_gate_units(paths: list[Any]) -> int:
    """Estimate pre-flight credit units from source page count where possible."""

    total = 0
    for raw in paths:
        path = raw if hasattr(raw, "read_bytes") else None
        if path is None:
            path = Path(str(raw))
        try:
            total += count_input_pages(path.read_bytes(), mime_for(path))
        except Exception:
            total += 1
    return total


def _charge_credits_in_tool() -> bool:
    raw = os.environ.get("LEDGR_CHARGE_CREDITS_IN_TOOL", "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _merge_validation(payload: dict[str, Any], extra: dict[str, object]) -> None:
    if extra:
        payload["validation_summary"] = {
            **payload.get("validation_summary", {}),
            **extra,
        }


def _build_pipeline_inject(overrides: dict[str, Any]) -> tuple[PipelineInject, dict[str, Any]]:
    """Build engine inject kwargs and LLM telemetry for the current call."""

    telemetry: dict[str, Any] = {
        "llm_call_count": 0,
        "models_used": [],
        "strong_model_used": False,
    }

    from invoice_processing.classify.document_classifier import classify_file
    from invoice_processing.extract.bank_statement_extractor import extract_bank_file
    from invoice_processing.extract.invoice_extractor import extract_file
    from invoice_processing.export.categorizer import categorize_invoice
    from invoice_processing.extract.process_invoice_document import process_invoice_document

    lite_name = lite_model()
    std_name = resolve_model("std")

    def _track(fn: Callable[..., Any], *, model_name: str, strong: bool = False) -> Callable[..., Any]:
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            telemetry["llm_call_count"] += 1
            if model_name not in telemetry["models_used"]:
                telemetry["models_used"].append(model_name)
            if strong:
                telemetry["strong_model_used"] = True
            return fn(*args, **kwargs)

        return wrapped

    base_inject: PipelineInject = {
        "classify_fn": classify_file,
        "extract_fn": extract_file,
        "bank_fn": extract_bank_file,
        "categorize_fn": categorize_invoice,
        # invoice_process_fn is the real spine entry point for invoice/receipt
        # extraction (document_engine.py uses this key). Must mirror the spine's
        # own default (process_invoice_document) so wrapping doesn't alter
        # behaviour when no override is provided.
        "invoice_process_fn": process_invoice_document,
    }

    # Apply overrides first
    for k, v in overrides.items():
        if v is not None:
            base_inject[k] = v

    # Wrap the active callables to track their telemetry
    if "classify_fn" in base_inject:
        base_inject["classify_fn"] = _track(base_inject["classify_fn"], model_name=lite_name)
    if "extract_fn" in base_inject:
        base_inject["extract_fn"] = _track(base_inject["extract_fn"], model_name=lite_name)
    if "bank_fn" in base_inject:
        base_inject["bank_fn"] = _track(base_inject["bank_fn"], model_name=std_name, strong=True)
    if "categorize_fn" in base_inject:
        # NOTE: categorize_invoice only calls the LLM when there are unresolved
        # COA lines *and* a COA is present. Wrapping it here may over-count by 1
        # when the categorizer resolves everything deterministically. Removing the
        # wrap entirely would under-count the real LLM cases, so we keep it.
        base_inject["categorize_fn"] = _track(base_inject["categorize_fn"], model_name=lite_name)
    if "invoice_process_fn" in base_inject:
        # This is the primary extraction call on the spine path (one call per
        # invoice/receipt document). Wrapping here fixes the previous gap where
        # all invoice-extraction LLM work went uncounted.
        base_inject["invoice_process_fn"] = _track(
            base_inject["invoice_process_fn"], model_name=lite_name
        )

    return base_inject, telemetry


def _empty_engine_result() -> EngineBatchResult:
    return EngineBatchResult(workbooks={}, docs=[], errors=[])


def _resolve_tax_policy_version(client: object) -> str | None:
    """Return the active jurisdiction policy version for the client, or None if unsupported.

    Falls back gracefully when the client has no ``region`` or the region is not in the
    supported SG/MY set (Plan 4.1's ``load_jurisdiction_policy`` raises ``ValueError``
    for unsupported markets).
    """
    region = getattr(client, "region", None)
    if not region or not isinstance(region, str):
        return None
    try:
        policy = load_jurisdiction_policy(region)
    except (ValueError, AttributeError):
        return None
    version = policy.get("policy_version") if isinstance(policy, dict) else None
    return version if isinstance(version, str) and version else None


_POLICY_VALIDATOR_JURISDICTIONS = {"SG", "MY"}


def _run_policy_validators(
    engine_result: Any,
    *,
    region: str | None,
    tax_registered: bool | None,
) -> list[dict]:
    """Run YAML review-rules validators over each processed doc.

    Fail-LOUD: policy-load errors and per-doc validator errors are surfaced as
    hard ``policy_validator_error`` violations rather than silently swallowed.
    Only runs for supported jurisdictions (SG/MY).

    Per-line ``invalid_tax_code`` violations are raised when a taxable line
    (non-zero ``gst_amount``) carries a blank resolved ERP ``tax_treatment``.
    """
    if not region:
        return []

    from ledgr_agent.policies.loader import _REGION_ALIASES

    market_key = _REGION_ALIASES.get(region.strip().upper())
    if market_key not in _POLICY_VALIDATOR_JURISDICTIONS:
        return []

    try:
        policy = load_jurisdiction_policy(region)
    except Exception as exc:
        return [
            {
                "id": "policy_validator_error",
                "severity": "hard_review",
                "file_name": "",
                "message": f"policy load failed for region={region!r}: {exc}",
            }
        ]

    # The SG policy carries registration.client_flag; MY currently does not.
    registration = policy.get("registration") or {}
    client_flag = registration.get("client_flag")
    if not client_flag:
        return []

    client_profile = {client_flag: bool(tax_registered)}

    violations: list[dict] = []
    for doc in getattr(engine_result, "docs", []):
        normalized = getattr(doc, "normalized", None)
        if normalized is None:
            continue
        file_name = Path(getattr(doc, "path", "") or "").name
        try:
            gst_total = float(getattr(normalized, "doc_gst_total") or 0.0)
            direction = str(getattr(normalized, "doc_type") or "")
            extracted = {"gst_total": gst_total, "direction_for_client": direction}
            doc_violations = validate_gst_registration_gate(
                policy, client_profile=client_profile, extracted=extracted
            )
            for v in doc_violations:
                violations.append({**v, "file_name": file_name, "message": v.get("message") or v.get("id", "")})
        except Exception as exc:
            violations.append(
                {
                    "id": "policy_validator_error",
                    "severity": "hard_review",
                    "file_name": file_name,
                    "message": f"validator raised for {file_name!r}: {exc}",
                }
            )

        # Per-line tax-code validity check: flag taxable lines with blank treatment.
        for line in getattr(normalized, "lines", []):
            gst_amount = getattr(line, "gst_amount", None)
            try:
                _gst_float = float(gst_amount) if gst_amount is not None else 0.0
            except (TypeError, ValueError):
                _gst_float = 0.0
            if gst_amount is None or abs(_gst_float) < 0.005:
                continue
            tax_treatment = getattr(line, "tax_treatment", None)
            if not tax_treatment or not str(tax_treatment).strip():
                desc = str(getattr(line, "description", "")) or "(unknown)"
                violations.append(
                    {
                        "id": "invalid_tax_code",
                        "severity": "hard_review",
                        "file_name": file_name,
                        "message": (
                            f"taxable line '{desc[:60]}' has blank tax_treatment"
                            f" (gst_amount={gst_amount})"
                        ),
                    }
                )

    return violations


def process_document_batch(tool_context: Any, paths: list[str], **inject: Any) -> dict[str, Any]:
    """Process a batch of document file paths (invoices, receipts, bank statements) for the active client.

    Args:
        paths: Absolute file paths on disk. In ADK web/agents-cli playground, pass
            ``[]`` when the user attached files with the upload button; the tool
            recovers uploaded PDF/image bytes from the session automatically.
        tool_context: Context injected by ADK providing access to the current session state.
        **inject: Seam for dependency injection in testing (e.g. classify_fn).
    """
    start_time = time.perf_counter()

    # 1. Resolve the client context state
    if tool_context is not None and getattr(tool_context, "state", None) is not None:
        state = tool_context.state
    else:
        # Fallback to playground context for local testing/eval
        from invoice_processing.shared_libraries.playground_context import playground_default_context
        state = playground_default_context().to_state()

    client = client_context_from_state(state)
    tax_policy_version = _resolve_tax_policy_version(client)

    existing_paths, missing_files, path_resolution = resolve_document_paths(
        tool_context,
        paths,
    )
    documents_skipped_before_llm = len(missing_files)
    source_files = [str(p) for p in existing_paths] if existing_paths else [str(p) for p in paths]
    gate_units = _estimate_gate_units(existing_paths) if existing_paths else len(source_files)

    firm_id = getattr(client, "firm_id", None) or getattr(client, "slack_team_id", None)
    credit_decision = _credit_gate(
        firm_id=firm_id,
        paths=source_files,
        required_units=gate_units,
    )
    credit_balance = credit_decision.get("balance")
    credit_remaining = (
        int(credit_balance)
        if firm_id and isinstance(credit_balance, int)
        else None
    )
    if not credit_decision.get("allowed", True):
        batch = map_engine_batch_to_contract(
            _empty_engine_result(),
            client=client,
            source_files=source_files,
            missing_files=missing_files,
            blocked_reason=credit_decision.get("reason", "zero_credit"),
            tax_policy_version=tax_policy_version,
            credits=CreditSummary(
                credits_estimated=gate_units,
                credits_used=0,
                credits_remaining=credit_remaining,
                credit_status="blocked",
            ),
        )
        payload = batch.model_dump()
        _merge_validation(
            payload,
            {
                **path_resolution,
                "credit_estimate": {
                    "gate_units": gate_units,
                    "gate_reason": credit_decision.get("reason"),
                    "charged_in_playground": False,
                },
            },
        )
        return payload

    if not existing_paths:
        blocked_reason = "no_source_files" if not paths else "no_readable_files"
        batch = map_engine_batch_to_contract(
            _empty_engine_result(),
            client=client,
            source_files=source_files,
            missing_files=missing_files,
            blocked_reason=blocked_reason,
            documents_skipped_before_llm=documents_skipped_before_llm,
            tax_policy_version=tax_policy_version,
        )
        payload = batch.model_dump()
        _merge_validation(
            payload,
            {
                **path_resolution,
                "credit_estimate": {
                    "gate_units": gate_units,
                    "charged_in_playground": False,
                },
            },
        )
        return payload

    # 2. Call the underlying procedural engine with dynamic telemetry.
    # Real ADK/tool runs use the multi-document spine so SOA packages fan out
    # into all embedded invoices. Legacy injected tests keep the deterministic
    # harness path because they stub ``extract_fn`` directly.
    pipeline_inject, telemetry = _build_pipeline_inject(inject)
    if "extract_fn" in inject:
        # Legacy harness path: pipeline.py::process_batch does not accept
        # ``invoice_process_fn`` — that key is spine-only. Strip it before
        # forwarding so injected-extract_fn tests keep working unchanged.
        legacy_inject = {k: v for k, v in pipeline_inject.items() if k != "invoice_process_fn"}
        engine_result = engine_process_batch(
            [str(p) for p in existing_paths],
            client,
            **legacy_inject,
        )
    else:
        engine_result = process_batch_with_document_spine(
            [str(p) for p in existing_paths],
            client,
            **pipeline_inject,
        )

    elapsed_ms = int((time.perf_counter() - start_time) * 1000)

    # Run YAML policy validators over normalised invoices before mapping.
    region = getattr(client, "region", None)
    tax_registered = state.get("tax_registered") if isinstance(state, dict) else getattr(state, "get", lambda k, d=None: d)("tax_registered")
    policy_violations = _run_policy_validators(
        engine_result,
        region=region,
        tax_registered=tax_registered,
    )

    # 4. Charge credits for delivered documents (charge-on-delivery rule).
    #
    # We count reconciled docs as "posted/delivered".  The idempotency key is
    # derived from firm_id + sorted source-file basenames so re-uploading the
    # exact same files never double-charges (InMemoryCreditStore deduplicates
    # on the (firm_id, idempotency_key) pair; the Firestore store will honour
    # the same contract).
    #
    # Fail-safe: a billing-service error must not lose the user's processed
    # work.  We swallow exceptions, log them, and set credit_status to
    # "not_checked" so the caller knows charging was skipped.
    firm_id = getattr(client, "firm_id", None) or getattr(client, "slack_team_id", None)
    posted_count = sum(
        1 for doc in engine_result.docs if doc.reconciled and not doc.note.startswith("ERROR")
    )

    if firm_id and posted_count > 0 and _charge_credits_in_tool():
        try:
            service = _get_credit_service()
            if service is not None:
                # Stable idempotency key: same firm + same file set → same key
                sorted_basenames = sorted(Path(f).name for f in source_files)
                idem_key = f"{firm_id}:{'|'.join(sorted_basenames)}"
                new_balance = service.deduct(
                    firm_id,
                    amount=posted_count,
                    reason="delivery",
                    idempotency_key=idem_key,
                )
                credits_summary = CreditSummary(
                    credits_estimated=gate_units,
                    credits_used=posted_count,
                    credits_remaining=int(new_balance),
                    credit_status="charged",
                )
            else:
                # Service unavailable — process succeeds, billing skipped
                credits_summary = CreditSummary(
                    credits_estimated=gate_units,
                    credits_used=0,
                    credits_remaining=credit_remaining,
                    credit_status="not_checked",
                )
        except Exception as exc:  # noqa: BLE001
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "Credit deduct failed (billing skipped, processing continues): %s", exc
            )
            credits_summary = CreditSummary(
                credits_estimated=gate_units,
                credits_used=0,
                credits_remaining=credit_remaining,
                credit_status="not_checked",
            )
    elif not firm_id:
        # Playground / eval path — not billable
        credits_summary = CreditSummary(
            credits_estimated=gate_units,
            credits_used=0,
            credits_remaining=None,
            credit_status="not_billable",
        )
    else:
        # firm_id present but nothing was posted (all errors/skipped)
        credits_summary = CreditSummary(
            credits_estimated=gate_units,
            credits_used=0,
            credits_remaining=credit_remaining,
            credit_status="estimated",
        )

    # 5. Map engine result to posted / skipped documents and extract review requests
    batch_result = map_engine_batch_to_contract(
        engine_result,
        client=client,
        source_files=source_files,
        missing_files=missing_files,
        llm_call_count=int(telemetry["llm_call_count"]),
        models_used=list(telemetry["models_used"]),
        strong_model_used=bool(telemetry["strong_model_used"]),
        elapsed_ms=elapsed_ms,
        documents_skipped_before_llm=documents_skipped_before_llm,
        tax_policy_version=tax_policy_version,
        policy_violations=policy_violations,
        credits=credits_summary,
    )

    payload = batch_result.model_dump()
    truth_report = document_truth_report(existing_paths, payload.get("export_rows") or [])
    expected_invoice_count = truth_report.get("expected_invoice_count")
    delivery_units = (
        int(expected_invoice_count)
        if isinstance(expected_invoice_count, int) and expected_invoice_count > 0
        else payload.get("documents_processed", 0)
    )
    _merge_validation(
        payload,
        {
            **path_resolution,
            "document_truth": truth_report,
            "credit_estimate": {
                "gate_units": gate_units,
                "estimated_delivery_units": delivery_units,
                "charged_in_playground": False,
                "rule": "gate by source page count; charge delivered invoice/receipt docs, bank pages in live Slack",
            },
        },
    )
    return payload
