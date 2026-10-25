"""Connection to the GTM CRM (the `gtm` schema of its own RDS Postgres, in the pavoot-ops stack).

The URL comes from GTM_DB_URL (tests, one-off runs) or the macOS Keychain item
`pavoot-gtm-db` (see db/002_gtm_role.sql). It is never written to a file or printed.
Keep transactions short: never hold one open across an LLM or HTTP call.
"""

from __future__ import annotations

import os
import subprocess

import psycopg
from psycopg.rows import dict_row


def url() -> str:
    if os.environ.get("GTM_DB_URL"):
        return os.environ["GTM_DB_URL"]
    try:
        return subprocess.run(["security", "find-generic-password", "-s", "pavoot-gtm-db", "-w"],
                              check=True, capture_output=True, text=True).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        raise SystemExit("No GTM database configured: set GTM_DB_URL or add the Keychain item "
                         "pavoot-gtm-db (see db/002_gtm_role.sql).")


def connect() -> psycopg.Connection:
    # autocommit: every write goes through an explicit, short `with conn.transaction()`.
    # prepare_threshold=None: no server-side prepared statements, so a connection pooler can't trip on them.
    return psycopg.connect(url(), row_factory=dict_row, autocommit=True, prepare_threshold=None,
                           options="-c search_path=gtm")
