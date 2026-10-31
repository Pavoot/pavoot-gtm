#!/bin/bash
# Apply new db/*.sql files (all idempotent) to the GTM database. Keeps the agent's login and the stored
# connection as they are. Run after pulling a change that adds a db/00N_*.sql file.
GTM_MIGRATE_ONLY=1 exec "$(dirname "$0")/setup_db.sh" "$@"
