#!/usr/bin/env bash
# CI: scan every commit a push or pull request adds, not only the final tree.
# Inputs (from the workflow): EVENT_NAME, BEFORE_SHA, HEAD_SHA, PR_BASE_SHA, PR_HEAD_SHA.
set -euo pipefail

zero="0000000000000000000000000000000000000000"
range=""
if [ "${EVENT_NAME:-}" = "pull_request" ]; then
    range="${PR_BASE_SHA}..${PR_HEAD_SHA}"
elif [ -n "${BEFORE_SHA:-}" ] && [ "${BEFORE_SHA}" != "$zero" ] \
    && git cat-file -e "${BEFORE_SHA}^{commit}" 2>/dev/null; then
    range="${BEFORE_SHA}..${HEAD_SHA}"
fi

if [ -n "$range" ]; then
    echo "secret scan: commits in $range"
    python scripts/secret_scan.py --range "$range"
else
    # A new branch, or a force push whose old head is gone: scan the whole history.
    echo "secret scan: every commit reachable from HEAD"
    python scripts/secret_scan.py --all-history
fi