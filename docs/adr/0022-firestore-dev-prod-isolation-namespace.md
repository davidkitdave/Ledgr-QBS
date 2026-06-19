# ADR-0022: Isolate dev/QA Firestore from production via a universal namespace prefix

Status: Accepted (2026-06-19)
Relates to: ADR-0018 (CI/CD, Cloud Run deploy), ADR-0012 (batch job queue / ledger writes).

## Context

Ledgr persists all durable state to Firestore: ADK sessions, per-client profiles,
channel→client mappings, FY ledger pointers, HITL interrupts, processing dedupe, and
the cross-instance ledger lease lock. The dev environment (local Socket-Mode bot +
`adk web` playground, AI Studio backend) and the production environment (Cloud Run,
Vertex, public Slack app) are distinguished only by `LEDGR_ENV`.

A QA audit on 2026-06-19 found that **dev/QA and production shared the exact same
Firestore collections**, so a test run could overwrite a real client's ledger pointer
or profile:

| Layer | Dev / QA (`.env`) | Production (`deploy.yml`) | Isolated? |
|---|---|---|---|
| GCP project | `GOOGLE_CLOUD_PROJECT=ledgr-qbs` | `PROJECT: ledgr-qbs` (deploy.yml:53, :277) | no — same |
| Firestore database | `(default)` (`firestore.Client()`, no `database=`) | `(default)` | no — same |
| Collection namespace | `LEDGR_FIRESTORE_NAMESPACE` unset | unset | no — none |

A namespace helper `accounting_agents.config._ns(name)` already existed (it returns
`f"{prefix}_{name}"` when `LEDGR_FIRESTORE_NAMESPACE` is set, else `name` unchanged),
but it was:

1. **Not enabled** — `LEDGR_FIRESTORE_NAMESPACE` was absent from the dev `.env`.
2. **Leaky** — applied only in `client_context.py` (`clients`, `channels`) and
   `hitl.py` (`interrupts`, `processed`). Three modules used **bare literals**:
   `sessions.py` (`sessions`), `ledger_store.py` (`clients`, ledger pointer +
   dedupe subcollections), and `lease_lock.py` (`clients` + lock subcollection).

The leak was also a latent **split-brain**: `ledger_store`/`lease_lock` keyed `clients`
bare while `client_context` namespaced it, so the moment a prefix was ever set, the
profile doc, ledger pointer, and lock would target *different* client documents.

## Decision

Route **every top-level Firestore collection** through `_ns()`, and enable the
namespace in dev. Subcollections are not prefixed — they inherit isolation from their
namespaced parent (`dev_clients/{id}/ledgers/...`).

- `sessions.py`: `collection(_ns(_ROOT_COLLECTION))` (top-level `sessions`); the
  `users`/`sessions`/`events` subcollections stay bare.
- `ledger_store.py`: `collection(_ns(_CLIENTS_COLLECTION))` at all call sites; the
  `ledgers` pointer and `dedup_stash` dedupe subcollections stay bare.
- `lease_lock.py`: `collection(_ns(_CLIENTS_COLLECTION))`; `ledger_locks` subcollection
  stays bare.
- `.env` (dev): `LEDGR_FIRESTORE_NAMESPACE=dev` → dev now writes `dev_clients`,
  `dev_sessions`, `dev_channels`, `dev_interrupts`, `dev_processed`.
- `tests/conftest.py` pins `LEDGR_FIRESTORE_NAMESPACE=""` before `load_dotenv()` so the
  dev `.env` value never bleeds into the hermetic suite.

Production leaves `LEDGR_FIRESTORE_NAMESPACE` unset, so prod collections are unchanged
(bare names) and **behaviour is byte-identical when the var is unset**. The isolation is
purely additive and reversible.

We chose the namespace-prefix mechanism (over a separate Firestore database or a separate
GCP project) deliberately: it is code-only, requires no GCP infrastructure changes, is
reversible, and gives complete logical separation for the current single-project setup.
A separate named database or project remains a future option for stronger physical
isolation (see Consequences).

## Consequences

- A dev/QA run can no longer read or clobber any production collection — the collection
  *names* no longer overlap.
- `clients`-rooted state (profile, ledger pointer, lock) is now provably consistent
  across the three stores (`tests/test_firestore_namespace.py` asserts they resolve to
  the same `_ns("clients")`).
- The isolation is **logical, within one GCP project + `(default)` database**. It depends
  on the dev `.env` carrying `LEDGR_FIRESTORE_NAMESPACE=dev`. `.env.example` documents
  this; a future hardening could fail-loud at dev startup if `LEDGR_ENV=dev`, the project
  equals the prod project, and the namespace is empty.
- Stronger physical isolation (a named Firestore database per env via
  `firestore.Client(database=...)`, or a dedicated dev GCP project) is deferred — it buys
  belt-and-suspenders separation at the cost of GCP setup, and can layer on top of the
  prefix without removing it.
