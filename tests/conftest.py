"""Throwaway Postgres for tests: initdb on a spare port, apply db/*.sql, connect as gtm_agent
(the same restricted login production uses), tear down after the session."""

import os
import shutil
import subprocess
import tempfile
import time

import psycopg
import pytest
from psycopg.rows import dict_row

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 55441


@pytest.fixture(scope="session")
def pg():
    if not shutil.which("initdb"):
        pytest.skip("initdb not installed")
    d = tempfile.mkdtemp()
    subprocess.run(["initdb", "-D", f"{d}/data", "-U", "postgres", "-A", "trust"], check=True, capture_output=True)
    subprocess.run(["pg_ctl", "-D", f"{d}/data", "-o", f"-p {PORT} -k {d}", "-l", f"{d}/log", "-w", "start"],
                   check=True, capture_output=True)
    admin = f"host={d} port={PORT} user=postgres dbname=postgres"
    try:
        with psycopg.connect(admin, autocommit=True) as c:
            c.execute("create table public.app_secret (x int); create role anon; create role authenticated")
            for f in ("001_gtm_schema.sql", "002_gtm_role.sql", "003_gtm_outreach.sql"):
                c.execute(open(os.path.join(ROOT, "db", f)).read())
        yield {"admin": admin, "agent": f"host={d} port={PORT} user=gtm_agent dbname=postgres"}
    finally:
        subprocess.run(["pg_ctl", "-D", f"{d}/data", "-m", "immediate", "stop"], capture_output=True)
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def conn(pg, monkeypatch):
    with psycopg.connect(pg["admin"], autocommit=True) as c:
        c.execute("truncate gtm.companies, gtm.people, gtm.experiments, gtm.sequences, gtm.touches, "
                  "gtm.replies, gtm.meetings, gtm.suppression, gtm.conflicts, gtm.brand_signals, gtm.learnings, "
                  "gtm.score_weights, gtm.content_metrics, gtm.daily_posts, gtm.weekly_reports restart identity cascade")
    monkeypatch.setenv("GTM_DB_URL", pg["agent"])
    with psycopg.connect(pg["agent"], row_factory=dict_row, autocommit=True, options="-c search_path=gtm") as c:
        yield c


@pytest.fixture
def anon(pg):
    import psycopg
    with psycopg.connect(pg["admin"], autocommit=True) as c:
        c.execute("alter role anon login")
    with psycopg.connect(pg["admin"].replace("user=postgres", "user=anon"), autocommit=True) as c:
        yield c
