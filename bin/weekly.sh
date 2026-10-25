#!/bin/bash
# Weekly learning run (launchd, Monday 07:30): last week's numbers -> learnings + weight proposals.
# The bot posts the report in #gtm at 10:00 San Francisco with ✅ buttons for weight changes.
set -uo pipefail
cd "$(dirname "$0")/.."
RUN="${RUN_DIR:-runs/week-$(date +%F)}"
mkdir -p "$RUN"
exec >>"$RUN/run.log" 2>&1
echo "== $(date) weekly learning"
PY=.venv/bin/python
$PY -m gtm.cli learn-stats ${GTM_WEEK:+--week "$GTM_WEEK"} --out "$RUN/stats.json" || { echo "learn-stats failed"; exit 1; }
sed -e "s#{{RUN}}#$RUN#g" prompts/learn.md > "$RUN/learn_prompt.md"
claude -p "$(cat "$RUN/learn_prompt.md")" --permission-mode dontAsk --allowedTools "Read" \
    --output-format text < /dev/null > "$RUN/learn_claude.txt" || echo "claude exited $?"
$PY -m gtm.cli learn-import "$RUN/learn_claude.txt" --stats "$RUN/stats.json" || echo "learn-import failed"
echo "== done $(date)"
