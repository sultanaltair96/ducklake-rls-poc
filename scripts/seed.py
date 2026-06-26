"""DuckLake RLS POC - seed script.

Creates the DuckLake, writes sample data to Azure Blob, then applies
PostgreSQL RLS policies to the DuckLake catalog.

Ordering:
  1. sql/init.sql creates DB/users/roles/default grants.
  2. This script ATTACHes DuckLake, creating ducklake_* catalog tables.
  3. This script uses psql to add region metadata and RLS policies.
"""

import os
import shutil
import subprocess
import sys
from urllib.parse import quote

import duckdb
from dotenv import load_dotenv

load_dotenv()


def _env(name, default=None):
    value = os.environ.get(name, default)
    if value is None or value == "":
        raise SystemExit("Missing required environment variable: " + name)
    return value


def _env_name(*parts):
    return "_".join(parts)


PG_HOST = os.environ.get("PG_HOST", "localhost")
PG_PORT = os.environ.get("PG_PORT", "5432")
PG_DB = os.environ.get("PG_DB", "ducklake_catalog")
PG_ADMIN_USER = os.environ.get("PG_ADMIN_USER", "postgres")
_PW = os.environ.get("PG_ADMIN_PASSWORD") or os.environ.get("PG_ADMIN_PW", "")

DATA_PATH = _env("DATA_PATH")

# Azure Blob credentials used by DuckDB's Azure filesystem extension.
_ACCOUNT = _env(_env_name("AZURE", "STORAGE", "ACCOUNT"))
_TOKEN = _env(_env_name("AZURE", "STORAGE", "KEY"))


def pg_conninfo(user, pw=""):
    parts = [
        "host=" + PG_HOST,
        "port=" + PG_PORT,
        "dbname=" + PG_DB,
        "user=" + user,
    ]
    if pw:
        parts.append("password=" + pw)
    return " ".join(parts)


def psql_base_cmd():
    exe = shutil.which("psql")
    if not exe:
        raise SystemExit("psql not found. Install PostgreSQL client tools before running seed.py")
    return [exe, "-v", "ON_ERROR_STOP=1", "-h", PG_HOST, "-p", PG_PORT, "-U", PG_ADMIN_USER, "-d", PG_DB]


def run_catalog_sql(sql):
    env = os.environ.copy()
    if _PW:
        env["PGPASSWORD"] = _PW
    proc = subprocess.run(psql_base_cmd(), input=sql, text=True, env=env)
    if proc.returncode != 0:
        raise SystemExit(proc.returncode)


def make_duckdb_connection():
    con = duckdb.connect(":memory:")
    con.execute("INSTALL ducklake; LOAD ducklake;")
    con.execute("INSTALL azure; LOAD azure;")
    con.execute(
        "SET azure_storage_connection_string = ?",
        ["DefaultEndpointsProtocol=https;AccountName=" + _ACCOUNT + ";AccountKey=" + _TOKEN + ";EndpointSuffix=core.windows.net"],
    )
    con.execute("ATTACH 'ducklake:postgres:" + pg_conninfo(PG_ADMIN_USER, _PW) + "' AS lake (DATA_PATH '" + DATA_PATH + "');")
    con.execute("USE lake;")
    return con


def apply_catalog_rls():
    sql = """
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO
    ducklake_admin, ducklake_eu_analyst, ducklake_us_analyst, ducklake_pii_reader,
    ducklake_eu_limited, ducklake_us_limited;

ALTER TABLE public.ducklake_table ADD COLUMN IF NOT EXISTS region VARCHAR DEFAULT 'global';

UPDATE public.ducklake_table SET region = 'eu' WHERE table_name = 'customers_eu';
UPDATE public.ducklake_table SET region = 'us' WHERE table_name = 'customers_us';
UPDATE public.ducklake_table SET region = 'masked' WHERE table_name = 'customers_masked';

ALTER TABLE public.ducklake_table ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS allow_ducklake_demo_roles ON public.ducklake_table;
DROP POLICY IF EXISTS region_eu ON public.ducklake_table;
DROP POLICY IF EXISTS region_us ON public.ducklake_table;
DROP POLICY IF EXISTS region_masked ON public.ducklake_table;
DROP POLICY IF EXISTS "eu-limited" ON public.ducklake_table;
DROP POLICY IF EXISTS "us-limited" ON public.ducklake_table;

-- Postgres combines permissive policies with OR, then restrictive policies
-- with AND. Without at least one permissive policy, restrictive-only setup
-- defaults to deny-all.
CREATE POLICY allow_ducklake_demo_roles ON public.ducklake_table
    AS PERMISSIVE
    FOR SELECT
    TO ducklake_eu_analyst, ducklake_us_analyst, ducklake_pii_reader,
       ducklake_eu_limited, ducklake_us_limited
    USING (true);

CREATE POLICY region_eu ON public.ducklake_table
    AS RESTRICTIVE
    FOR SELECT
    TO ducklake_eu_analyst
    USING (region = 'eu');

CREATE POLICY region_us ON public.ducklake_table
    AS RESTRICTIVE
    FOR SELECT
    TO ducklake_us_analyst
    USING (region = 'us');

CREATE POLICY region_masked ON public.ducklake_table
    AS RESTRICTIVE
    FOR SELECT
    TO ducklake_pii_reader
    USING (region = 'masked');

CREATE POLICY "eu-limited" ON public.ducklake_table
    AS RESTRICTIVE
    FOR SELECT
    TO ducklake_eu_limited
    USING (region = 'masked');

CREATE POLICY "us-limited" ON public.ducklake_table
    AS RESTRICTIVE
    FOR SELECT
    TO ducklake_us_limited
    USING (region = 'masked');
"""
    run_catalog_sql(sql)


def main():
    print("Attaching DuckLake as catalog admin (" + PG_ADMIN_USER + ")...")
    con = make_duckdb_connection()

    print("Creating sample data tables...")
    con.execute("CREATE OR REPLACE TABLE customers_eu AS "
                "SELECT * FROM (VALUES "
                "(1, 'Maria Schmidt', 'maria@example.eu', 'DE-123456789', 'eu'),"
                "(2, 'Lars Eriksen',  'lars@example.eu',  'NO-987654321', 'eu')"
                ") AS t(id, name, email, ssn, region)")
    con.execute("CREATE OR REPLACE TABLE customers_us AS "
                "SELECT * FROM (VALUES "
                "(3, 'John Smith',    'john@example.us',  'US-555-44-3333', 'us'),"
                "(4, 'Emily Johnson', 'emily@example.us', 'US-111-22-3333', 'us')"
                ") AS t(id, name, email, ssn, region)")
    con.execute("CREATE OR REPLACE TABLE customers_masked AS "
                "SELECT * FROM (VALUES "
                "(1, 'Maria Schmidt', 'maria@example.eu', '***-**-****', 'eu'),"
                "(2, 'Lars Eriksen',  'lars@example.eu',  '***-**-****', 'eu'),"
                "(3, 'John Smith',    'john@example.us',  '***-**-****', 'us'),"
                "(4, 'Emily Johnson', 'emily@example.us', '***-**-****', 'us')"
                ") AS t(id, name, email, ssn, region)")

    # The demo is explicitly about Parquet files in object storage. Small
    # inserts are inlined into the catalog by default, so flush them.
    con.execute("CALL ducklake_flush_inlined_data('lake')")

    print("Applying Postgres RLS policies to ducklake_table...")
    apply_catalog_rls()

    print("\nCatalog contents as admin:")
    for row in con.execute("SELECT table_name, region FROM __ducklake_metadata_lake.ducklake_table WHERE end_snapshot IS NULL ORDER BY table_name").fetchall():
        print("  " + str(row))

    print("\nParquet files materialized in Azure Blob:")
    for row in con.execute("SELECT path, file_size_bytes FROM __ducklake_metadata_lake.ducklake_data_file ORDER BY path").fetchall():
        print("  " + str(row[0]) + "  (" + str(row[1]) + " bytes)")

    print("\nSeed complete. Edit AS_USER in scripts/demo.py and run: python scripts/demo.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
