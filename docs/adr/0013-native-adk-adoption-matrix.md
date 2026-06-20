# ADR-0013: Native ADK adoption matrix (chat lane)

**Status:** Accepted (2026-06-17)

**Context:** Phase P6 of the helpful-agent plan audited ADK native tools against Ledgr's
22-tool chat surface. The goal was to avoid rebuilding ledger/explain/write logic with
built-ins that cannot coexist with custom ``FunctionTool``s on the same agent.

## Decision

| Native ADK capability | Adopt for chat? | Notes |
|----------------------|-----------------|-------|
| ``LlmAgent`` + ``Runner`` | **Yes** (already) | Standalone ``assistant_app`` per ADR-0008 |
| ``FunctionTool`` + ``require_confirmation=True`` | **Yes** (already) | Write tools (ADR-0009) |
| ``before_model_callback`` | **Yes** (already) | Onboarding guardrail |
| ``ToolContext`` + ``state`` / ``state_delta`` | **Yes** (already) | Runner injects; tools stay pure |
| ``FirestoreSessionService`` | **Yes** (already) | HITL resume |
| Instruction ``{+key?+}`` templating | **Yes** (P5) | ``_BASE_INSTRUCTION`` + runner counts |
| ``ReflectAndRetryToolPlugin`` | **Yes** (P7) | Retry on tool ``error`` / ``not_found`` JSON |
| ``BuiltInCodeExecutor`` | **No** | Cannot mix with custom tools on same agent |
| ``google_search`` / ``url_context`` | **No** on chat agent | Wrong data plane; optional future ``AgentTool`` sub-agent |
| RAG / ``file_search`` on workbook rows | **No** | Structured ledger rows, not prose corpus |
| ``load_memory`` / ``PreloadMemoryTool`` | **Optional later** | Conversational recall ≠ ``entity_memory`` rules |
| MCP Toolbox → Firestore | **Optional later** | Runner-side reads only |
| ``LongRunningFunctionTool`` | **No** | Graph HITL replaces prototype in ``tools.py`` |
| ADK Skills (``SkillToolset``) | **Optional** (P8) | Playbooks in ``accounting_agents/skills/``; not wired by default |

## Custom code that must stay

- **Read tools** — deterministic ledger math (`bank_totals`, `pnl_for_fy`, …)
- **Explain tools** — call ``invoice_processing`` engines (same logic as pipeline)
- **Write tools** — ADR-0009 confirmation + tax re-derivation
- **Introspect tools** — read runner-injected ``processing_log``, ``document_sessions``, ``pending_reviews``

## Prototype deprecation

``accounting_agents/tools.py`` is an early ADK prototype (LLM Excel mapping,
``fetch_client_profile`` inside a tool, ``LongRunningFunctionTool`` stub). It is
**not imported** by the production chat or document paths. Do not wire it into
``assistant_agent``. Prefer ``ledger_store`` + runner-injected profile.

## Consequences

- Chat agent tool list remains custom ``FunctionTool``s only (no built-in Gemini tools).
- Optional memory and Skills can be enabled behind env flags without changing core tools.
- Competitor alignment: hybrid LLM orchestration + deterministic policy (Digits/Ramp pattern).
