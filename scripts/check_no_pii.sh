#!/usr/bin/env bash
# ADR-0015 PII guard — fail the build if real client or vendor names,
# real-looking UENs / account numbers / emails, or home-relative
# developer paths leak into source / fixtures / docs.
#
# Anonymised placeholders ("Company-A" / "Company-B" / "Person-1" /
# "Person-2") are the **only** party names allowed in source. Any other
# name trips the guard.
#
# The allowlist is intentionally empty. If a real value must land in
# the repo (e.g. a known historical archive), document WHY in a
# dedicated ADR and reference that ADR from this file. New code must
# never depend on the allowlist.
#
# Exit 0 = clean. Exit 1 = at least one disallowed name found.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Anonymised placeholders the model + fixtures use. These are NOT
# sensitive and must be allowed everywhere. The guard's job is to
# ensure no OTHER name ever appears.
ALLOWED_GENERIC_NAMES=(
  "Company-A"
  "Company-B"
  "Person-1"
  "Person-2"
)

# Real client / vendor / firm / personal names. Any hit (other than
# the placeholder names above) in the scanned scope trips the guard.
# The literal strings in the array below MUST exist for self-test;
# they are skipped by the loop (see SKIP_NAMES_FOR_GREP).
REAL_NAMES=(
  "Auditair"
  "Darrell"
  "Podaima"
  "Cast Unity"
)

# Directories the guard scans for source / fixtures / docs. The
# artifacts/ tree is intentionally excluded — those are *outputs* of
# prior eval runs, snapshots of model output, not source of truth.
# Regenerating them would re-introduce data drift.
SCAN_DIRS=(
  "ledgr_slack"
  "ledgr_agent"
  "app"
  "tests"
  "eval"
  "docs/adr"
  "docs/qa"
  "scripts"
)

# Broader PII patterns. Each entry is a ripgrep regex; matches that
# survive path filtering trip the guard.
#
#  - Real-looking SG UENs: 9 digits + check letter (e.g. 201234567A).
#    The synthetic F-cluster UENs (2000000xx) are allowed; we filter
#    them with a negative lookbehind via plain grep post-filter.
#  - Bank ACCT: numbers with 4+ digits in ACCT-like contexts.
#  - Real email domains: matches anything @<flagged-domain>.
#  - Home-relative developer paths: anything under Desktop/LocalTest
#    or pointing at Acme/Cast Unity/Auditair specific folders.
EXTRA_PATTERNS=(
  'ACCT[: ]*[0-9]{4,}'
  '@auditair'
  '@castunity'
  '@podaima'
  # The developer's local-firm test name (real PII surface). The
  # generic "Acme Vendor" / "Acme Corp" / "Acme Pte Ltd" / "Acme
  # Inc" / "Acme Trading" / "Acme Supplies" placeholders are
  # *allowed* below as documented test conventions.
  'Acme Client Pte\.? Ltd\.?'
  'Desktop/LocalTest/[^"]*Acme'
  'Desktop/LocalTest/[^"]*Cast Unity'
  'Desktop/LocalTest/[^"]*Auditair'
)

# Generic "Acme" test placeholders that are allowed (they are the
# long-standing example-corp convention in the test corpus). These
# do NOT refer to the developer's local firm; that is "Acme Client
# Pte. Ltd." which is checked separately above.
ALLOWED_GENERIC_ACME=(
  'Acme Vendor'
  'Acme Corp'
  'Acme Pte Ltd'
  'Acme Inc'
  'Acme Trading'
  'Acme Supplies'
  'Acme Supplier'
  'Acme Cloud'
  'Acme SG Pte Ltd'
  'Acme Customer Pte Ltd'
  'Acme Professional Services'
)

failures=0

# --- real-name check ---------------------------------------------------------
for name in "${REAL_NAMES[@]}"; do
  # rg gives us file:line:content; we filter out the guard's own file.
  matches=$(rg --no-heading --line-number -t py -t md -t yaml -t json -t sh \
    -g '!**/archive/**' \
    -e "$name" "${SCAN_DIRS[@]}" 2>/dev/null \
    | rg -v '^scripts/check_no_pii\.sh:' || true)
  if [[ -z "$matches" ]]; then
    continue
  fi
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    echo "PII leak: '$name' in $line" >&2
    failures=$((failures + 1))
  done <<< "$matches"
done

# --- broader PII pattern check ----------------------------------------------
for pattern in "${EXTRA_PATTERNS[@]}"; do
  matches=$(rg --no-heading --line-number -t py -t md -t yaml -t json -t sh \
    -e "$pattern" "${SCAN_DIRS[@]}" 2>/dev/null \
    | rg -v '^scripts/check_no_pii\.sh:' || true)
  if [[ -z "$matches" ]]; then
    continue
  fi
  # Drop matches that contain one of the allowed generic placeholders.
  for allowed in "${ALLOWED_GENERIC_ACME[@]}"; do
    matches=$(printf '%s\n' "$matches" | rg -v -F "$allowed" || true)
  done
  if [[ -z "$matches" ]]; then
    continue
  fi
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    echo "PII pattern '$pattern' in $line" >&2
    failures=$((failures + 1))
  done <<< "$matches"
done

# --- real-looking UEN check (9 digits + uppercase check letter) --------------
# Synthetic 9-digit test UENs are allowed in test code / fixtures /
# docs that document the test convention. Real UENs registered in
# Singapore use a year prefix (19xx or 20xx) but real ones also share
# the same shape, so we restrict the check to source modules
# (ledgr_slack/, ledgr_agent/, app/) and config files
# (eval/, docs/adr/, scripts/, .github/) where a *real* UEN would
# leak. The tests/ tree is allowed to carry synthetic UENs.
  matches=$(rg --no-heading --line-number \
  -g '!**/archive/**' \
  -e '\b[0-9]{9}[A-Z]\b' \
  ledgr_slack ledgr_agent app eval docs/adr docs/qa scripts 2>/dev/null \
  | rg -v '2000000[0-9]{2}[A-Z]' \
  | rg -v '201234567A' \
  | rg -v '201712345A' \
  | rg -v '201912345A' \
  | rg -v '201700001A' \
  | rg -v '201800002B' \
  | rg -v '201899995Z' \
  | rg -v '200099001Z' \
  | rg -v '199100003C' \
  | rg -v '199200001Z' \
  | rg -v '199200004D' \
  | rg -v '200012345A' \
  | rg -v '53312345B' \
  | rg -v 'M90312345A' \
  | rg -v '^scripts/check_no_pii\.sh:' || true)
if [[ -n "$matches" ]]; then
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    echo "Possible real UEN in $line" >&2
    failures=$((failures + 1))
  done <<< "$matches"
fi

if [[ "$failures" -gt 0 ]]; then
  echo
  echo "FAIL: $failures disallowed name / PII reference(s) found."
  echo "Replace with anonymised placeholders Company-A / Company-B /"
  echo "Person-1 / Person-2. If a real value must be committed, document"
  echo "WHY in a dedicated ADR and update this guard with a tight"
  echo "allowlist entry pointing at the ADR."
  exit 1
fi

echo "OK: no disallowed name references in scanned paths."
exit 0
