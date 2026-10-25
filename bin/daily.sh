#!/bin/bash
# Daily GTM run on Ana's Mac (launchd 08:30): plan + draft first (so the 09:00 #gtm post has them),
# then prospect for tomorrow. Cards drafted after the post still join that day's thread in #gtm.
# A failing step is logged and the rest still runs.
set -uo pipefail
cd "$(dirname "$0")/.."
DATE=$(date +%F)
RUN="${RUN_DIR:-runs/$DATE}"
mkdir -p "$RUN"
PY=.venv/bin/python

draft() {
    echo "== $(date) plan + drafts"
    $PY -m gtm.cli rescore || echo "rescore failed"
    $PY -m gtm.cli plan || echo "plan failed"
    $PY -m gtm.cli export-due --out "$RUN/due.json" || { echo "export-due failed"; return 1; }
    if [ "$($PY -c "import json;print(len(json.load(open('$RUN/due.json'))['touches']))")" = "0" ]; then
        echo "nothing due today"; return 0
    fi
    sed -e "s#{{RUN}}#$RUN#g" prompts/draft.md > "$RUN/draft_prompt.md"
    # Read-only run: it may read due.json and the fact sheet, nothing else; the answer ends with the JSON.
    claude -p "$(cat "$RUN/draft_prompt.md")" \
        --permission-mode dontAsk --allowedTools "Read" \
        --output-format text < /dev/null > "$RUN/draft_claude.txt" || echo "claude exited $?"
    $PY -m gtm.cli import-drafts "$RUN/draft_claude.txt" || echo "some drafts were blocked (see above)"
}

draft >>"$RUN/run.log" 2>&1
RUN_DIR="$RUN" bin/prospect.sh || echo "prospecting failed (see $RUN/run.log)" >>"$RUN/run.log"
echo "== done $(date)" >>"$RUN/run.log"
