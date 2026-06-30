# ERP import skills — ADK directory layout

Each ``erp-<system>/`` folder is an [ADK Agent Skill](https://adk.dev/skills/index.md)
directory:

- ``SKILL.md`` — human-readable metadata and editing notes (not loaded by Python).
- ``assets/profile.yaml`` — declarative column map for that ERP's import file.

``build_sheets`` (light path) and ``ledgr_slack/export/exporters.py`` load
profiles through the single loader in ``ledgr_agent/internal/skill_profiles.py``.
A malformed or missing profile fails **loud at load**, never silently producing wrong
columns.

## Directories

| Directory           | `system`      | `exporter` | Notes                                           |
| ------------------- | ------------- | ---------- | ----------------------------------------------- |
| `erp-qbs/`          | `qbs`         | `builtin`  | Native QBS Ledger format (no tax-code column).  |
| `erp-xero/`         | `xero`        | `builtin`  | Xero import columns + `*`-marked required cols. |
| `erp-autocount/`    | `autocount`   | `profile`  | AutoCount AP/AR import (21 cols each).           |
| `erp-sql-account/`  | `sql_account` | `profile`  | SQL Account `SLPH_Invoice_Cash_Debit_Credit`.   |

## Schema

There are two `exporter` flavours. Both share the column-list keys; they differ
in how columns map to logical fields.

### Common keys (all skills)

- `software_name` (str) — the human display name (`Sys_Config` SOFTWARE value).
- `system` (str) — the canonical exporter key (`qbs` / `xero` / `autocount` /
  `sql_account`). Must be unique across the directory.
- `exporter` (str) — `builtin` or `profile` (selects which exporter class loads
  the skill; see below).
- `purchase_cols` (list[str]) — ordered column headers for the Purchase/AP sheet.
- `sales_cols` (list[str]) — ordered column headers for the Sales/AR sheet.

### `exporter: builtin` (QBS, Xero)

These ERPs use the base `LedgerExporter` row builders (`QbsLedgerExporter`,
`XeroLedgerExporter`). Their row dicts are built in Python; the skill supplies
only the column ordering and the logical-field lookup:

- `logical_fields` (dict[str, str]) — `{column_name: logical_field}` covering
  **both** sheets. `column_for_field(field, doc_type)` walks the relevant
  `*_cols` list and returns the first column whose `logical_fields` entry equals
  the requested logical field. (e.g. QBS sales `"Amount"` → `sub_total`.)

### `exporter: profile` (AutoCount, SQL Account)

These ERPs are driven entirely by data through `ProfileLedgerExporter`. In
addition to the common keys they declare:

- `purchase_sheet` / `sales_sheet` (str) — worksheet tab names (default
  `Purchase` / `Sales`).
- `purchase_fields` / `sales_fields` (dict[str, str]) — `{column: context_key}`
  map. The exporter builds a per-line context dict (`invoice_number`,
  `sub_total`, `tax_code`, `creditor_code`, `qty`, …) and writes each column
  from its mapped context key. This map is also inverted by `column_for_field`.
- `purchase_constants` / `sales_constants` (dict[str, Any]) — fixed column
  values applied **after** field mapping (e.g. AutoCount `DocNo: "<<New>>"`,
  `JournalType: "PURCHASE"`, `InclusiveTax: "F"`).
- `required_purchase` / `required_sales` (list[str]) — columns that must be
  non-empty in every exported row (drives the "flag, don't drop" readiness check).
- `purchase_preview_cols` / `sales_preview_cols` (list[str], optional) — a
  curated ≤20-column subset for the Slack `data_table` preview. Falls back to
  the full `*_cols` list when absent.

## Behaviour contract

Exporter output must stay **byte-identical** — the golden-format acceptance
tests (`tests/test_erp_golden_format.py`, `tests/test_erp_exporters.py`,
`tests/test_app_blocks.py`) are the bar. Editing a column name, order, or
mapping in `assets/profile.yaml` directly changes the generated import file, so
any change must be verified against those tests.
