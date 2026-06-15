# Anonymisation Note

Raw source documents (PDFs, XLSX workbooks) live exclusively on the developer's
local machine and are never committed to this repository.

The evalset file `ledgr.evalset.json` contains only:

- **Expected output values** (tax treatment codes, tool call names, account
  categories, response anchor strings) used by the ADK `AgentEvaluator` to
  score each case.
- **Path references** pointing to documents under `~/Downloads/` and
  `~/Desktop/LocalTest/` on the developer's machine. These paths are inert
  strings — the evaluator does not open files at those paths directly; they
  are carried in `user_content.parts[].text` as context for the agent under
  test.
- **Anonymised firm names** (`Test Firm`, `Test Sub-Client`, `Test Firm
  GST-Reg`, etc.) in all human-readable fields. Real client or vendor names
  must not appear in any committed file (project memory rule: `no-real-client-
  data-in-repo`).

If you need to add a new eval case that references a real document, place the
document under `~/Desktop/LocalTest/` (or equivalent local path), reference
it by path in `user_content`, and use an anonymised placeholder for any firm
or vendor name that appears in the case JSON.
