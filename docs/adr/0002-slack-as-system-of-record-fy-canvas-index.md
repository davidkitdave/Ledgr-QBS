# 0002 — Slack channel is the system of record; FY "folders" are a Canvas index

- **Status:** Accepted
- **Date:** 2026-06-13
- **Deciders:** David (developer)

## Context

The goal: documents dropped in a client channel should be kept tidy, filed by
financial year, so a channel doesn't become a mess. The desired end-state (per the
developer's screenshots) is that **the documents and the accumulating workbook live
in the Slack channel's Files tab** — not in external object storage.

Two facts shaped the decision:

1. The code currently archives every source doc and workbook to **GCS**
   (`app/archive.py`), and the cross-month workbook continuity (and `/ledgr export`)
   **reads the prior workbook from GCS** (`archive.get_workbook`, `slack_app.py`).
   So GCS is currently load-bearing for the "update the same workbook without
   duplicating" behaviour.
2. Slack's per-channel **Files-tab folders** exist in the UI — but research across
   the Slack changelog (2015–2026), the Web API methods reference, and help docs
   confirms there is **no Web API** to create a folder or move a file into one.
   A bot cannot create or populate these folders. Folders are a manual UI feature
   (the channel `+` → Folder), holding ≤100 items (canvases, lists, files, links).

Project memory also frames Ledgr storage as working/ephemeral — the client's own
accounting software is the authoritative book of record — so Ledgr does not need a
durable multi-year archive of its own.

## Decision

- **The Slack channel's Files tab is Ledgr's system of record** for source
  documents and workbooks. GCS is retired as the record.
- The prior-workbook retrieval (continuous append + `/ledgr export`) is **re-pointed
  to fetch from Slack Files** instead of `archive.get_workbook`.
- "Filing into the right FY folder" is realised as a **per-FY Canvas index**: the
  Teammate maintains a channel Canvas with one section per financial year, each
  listing the client's documents (vendor · date · type · status) with a permalink
  to the file. The Canvas is the tidy "folder view"; the raw chat may stay noisy.
- Creating an actual Slack folder and dragging the Canvas + files in remains an
  **optional manual** step for the human, because no API supports automating it.

## Consequences

- The dependency the developer wanted to drop (GCS) is dropped; storage matches the
  user's mental model (everything in the channel).
- Continuity now depends on **Slack file retention** and the workspace staying
  alive. Acceptable because the authoritative books live in the client's accounting
  software, not Ledgr.
- The Teammate gains a real "where are my docs / what's filed under FY2025" surface
  via the Canvas, without pretending to do something the API can't.
- If Slack ever ships a folders Web API, automating the literal folder becomes a
  drop-in enhancement on top of the Canvas index.

## Alternatives considered

- **Keep GCS as the record, Slack shows links** — most reliable, but contradicts
  the "documents live in the channel" goal; rejected.
- **Slack-native + GCS as a silent backup** — more robust, but re-introduces the
  dependency we set out to drop; deferred (revisit if Slack retention proves risky).
- **A separate `#client-fy2025` channel per year** — native-feeling folders, but
  multiplies channels per client and fragments the conversation; rejected.
- **Bookmarks/pins per FY** — too thin; no per-document listing; rejected in favour
  of the Canvas.
