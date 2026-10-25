#!/bin/bash
# One-time (safe to re-run): load the GTM CRM schema into the RDS Postgres that the pavoot-ops stack
# created, (re)create the gtm_agent login with a new random password, and store its connection string
# in the Mac Keychain (pavoot-gtm-db) and Parameter Store (/pavoot-ops/gtm-db-url) for the bot.
# Needs: AWS credentials for the Pavoot account (aws sso login), psql. Prints no password.
#
# Test hooks (no AWS): GTM_SETUP_HOST + GTM_SETUP_ADMIN_PW skip the AWS lookups, GTM_SETUP_NO_STORE=1
# skips Keychain/Parameter Store, GTM_SETUP_SSLMODE overrides sslmode (default require).
set -euo pipefail
cd "$(dirname "$0")/.."
REGION=${AWS_REGION:-us-west-2}
STACK=${STACK:-pavoot-ops}

out() {
    aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
        --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}

HOST=${GTM_SETUP_HOST:-}
ADMIN_PW=${GTM_SETUP_ADMIN_PW:-}
if [ -z "$HOST" ]; then
    HOST=$(out GtmDbEndpoint)
    SECRET=$(out GtmDbAdminSecret)
    if [ -z "$HOST" ] || [ "$HOST" = "None" ]; then
        echo "The database isn't there yet: run ./deploy.sh in pavoot-ops first (it takes ~10 minutes)."
        exit 1
    fi
    ADMIN_PW=$(aws secretsmanager get-secret-value --region "$REGION" --secret-id "$SECRET" \
        --query SecretString --output text | python3 -c "import json,sys; print(json.load(sys.stdin)['password'])")
fi
PORT=${GTM_SETUP_PORT:-5432}
SSLMODE=${GTM_SETUP_SSLMODE:-require}
AGENT_PW=$(openssl rand -hex 24)

export PGPASSWORD="$ADMIN_PW" PGSSLMODE="$SSLMODE"
P=(psql -h "$HOST" -p "$PORT" -U gtm_admin -d gtm -v ON_ERROR_STOP=1 -q)
echo "Loading the schema into $HOST ..."
"${P[@]}" -f db/001_gtm_schema.sql 2>&1 | grep -v NOTICE || true
sed "s/CHANGE_ME/$AGENT_PW/" db/002_gtm_role.sql | "${P[@]}" 2>&1 | grep -v NOTICE || true
"${P[@]}" -c "alter role gtm_agent with password '$AGENT_PW'"      # also when the login already existed
"${P[@]}" -f db/003_gtm_outreach.sql 2>&1 | grep -v NOTICE || true
"${P[@]}" -At -c "select count(*) from pg_tables where schemaname = 'gtm'" | grep -qE '^1[0-9]$' \
    || { echo "The gtm tables are missing after loading; see the errors above."; exit 1; }
unset PGPASSWORD

URL="postgresql://gtm_agent:${AGENT_PW}@${HOST}:${PORT}/gtm?sslmode=${SSLMODE}"
if [ "${GTM_SETUP_NO_STORE:-}" = "1" ]; then
    echo "$URL" > "${GTM_SETUP_URL_FILE:-/dev/null}"
    echo "Done (test mode: nothing stored)."
    exit 0
fi
security add-generic-password -U -a gtm_agent -s pavoot-gtm-db -w "$URL"
aws ssm put-parameter --region "$REGION" --name /pavoot-ops/gtm-db-url --type SecureString \
    --value "$URL" --overwrite > /dev/null
echo "Done: schema loaded, gtm_agent created, connection saved in your Keychain and for the bot."
echo "Check: .venv/bin/python -m gtm.cli stats"
