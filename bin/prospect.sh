#!/bin/bash
# Daily prospecting: YC candidates -> Claude research (headless) -> import into the GTM CRM.
# Run by hand or by launchd (launchd/com.pavoot.gtm-prospect.plist). Logs to runs/<date>/run.log.
set -euo pipefail
cd "$(dirname "$0")/.."
DATE=$(date +%F)
RUN="${RUN_DIR:-runs/$DATE}"
mkdir -p "$RUN"
exec >>"$RUN/run.log" 2>&1
echo "== $(date) prospecting run"

PY=.venv/bin/python
# Run size: ~7-8 companies x up to 3 contacts ~ 20 new people a day.
YC_N=${YC_N:-4}; NONYC_N=${NONYC_N:-4}; FIND_BUDGET=${FIND_BUDGET:-8}
if [ -s "$RUN/prospects.json" ]; then
    echo "prospects.json already exists for $DATE; importing only"
else
    $PY -m gtm.cli yc-candidates --limit "$YC_N" --out "$RUN/yc.json"
    $PY -m gtm.cli known-domains --out "$RUN/known_domains.txt"
    sed -e "s#{{RUN}}#$RUN#g" -e "s/{{YC_N}}/$YC_N/g" -e "s/{{NONYC_N}}/$NONYC_N/g" \
        -e "s/{{FIND_BUDGET}}/$FIND_BUDGET/g" prompts/prospect.md > "$RUN/prompt.md"
    # Read-only run: no file writes, no Bash, no sending. The answer ends with the JSON.
    claude -p "$(cat "$RUN/prompt.md")" \
        --permission-mode dontAsk \
        --allowedTools "Read" "WebSearch" "WebFetch" "mcp__pavoot__find_people" \
        --output-format text < /dev/null > "$RUN/claude.txt" || echo "claude exited $?"
    $PY -m gtm.cli extract-json "$RUN/claude.txt" --out "$RUN/prospects.json" || true
fi

if [ ! -s "$RUN/prospects.json" ]; then
    echo "no prospects.json produced; see $RUN/claude.txt"
    exit 1
fi
$PY -m gtm.cli import "$RUN/prospects.json"
echo "== done $(date)"
