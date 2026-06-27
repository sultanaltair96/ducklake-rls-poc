"""DuckLake RLS POC - seed script for the single-table policy model.

This file is the "build the demo lake" script. It does three jobs:

1. Connect DuckDB to a DuckLake catalog stored in PostgreSQL.
2. Create one canonical DuckLake table named `customers` in Azure Blob.
3. Apply PostgreSQL Row-Level Security (RLS) to DuckLake's catalog table.

The key idea in this version:

    PostgreSQL is the catalog/discovery control plane.
    Azure Blob is the data plane.
    demo.py is the row-filtering and masking policy layer.

Unlike the first POC shape, this version does not create separate physical
`customers_eu`, `customers_us`, and `customers_masked` tables. That avoids data
duplication. Everyone reads from one table, while demo.py chooses safe SELECT
lists and WHERE clauses per persona.
"""

import os
import shutil
import subprocess
import sys

import duckdb
from dotenv import load_dotenv

# Load variables from .env into os.environ. This keeps secrets and local setup
# out of the source code. See .env.example for the expected keys.
load_dotenv()


def _env(name, default=None):
    """Read a required environment variable with a clearer error message."""
    value = os.environ.get(name, default)
    if value is None or value == "":
        raise SystemExit("Missing required environment variable: " + name)
    return value


def _env_name(*parts):
    """Build environment variable names without hardcoding long strings."""
    return "_".join(parts)


# PostgreSQL connection settings. The database named by PG_DB is the DuckLake
# catalog database. It stores metadata, not the customer data itself.
PG_HOST = os.environ.get("PG_HOST", "localhost")
PG_PORT = os.environ.get("PG_PORT", "5432")
PG_DB = os.environ.get("PG_DB", "ducklake_catalog")
PG_ADMIN_USER = os.environ.get("PG_ADMIN_USER", "postgres")
_PW = os.environ.get("PG_ADMIN_PASSWORD") or os.environ.get("PG_ADMIN_PW", "")

# Where DuckLake writes Parquet data files. For Azure Blob this is an az:// URI,
# e.g. az://ducklake-one/. DuckDB's azure extension handles the HTTPS calls.
DATA_PATH = _env("DATA_PATH")

# Azure Blob credentials used by DuckDB's Azure filesystem extension.
_ACCOUNT = _env(_env_name("AZURE", "STORAGE", "ACCOUNT"))
_TOKEN = _env(_env_name("AZURE", "STORAGE", "KEY"))

DEMO_ROLES = """
    ducklake_eu_analyst, ducklake_us_analyst, ducklake_pii_reader,
    ducklake_eu_limited, ducklake_us_limited
"""


def pg_conninfo(user, pw=""):
    """Return a libpq-style connection string for DuckLake's Postgres catalog."""
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
    """Base psql command used for catalog-side SQL changes."""
    exe = shutil.which("psql")
    if not exe:
        raise SystemExit("psql not found. Install PostgreSQL client tools before running seed.py")
    return [exe, "-v", "ON_ERROR_STOP=1", "-h", PG_HOST, "-p", PG_PORT, "-U", PG_ADMIN_USER, "-d", PG_DB]


def run_catalog_sql(sql):
    """Run SQL against PostgreSQL's DuckLake catalog as the admin user."""
    env = os.environ.copy()
    if _PW:
        env["PGPASSWORD"] = _PW
    proc = subprocess.run(psql_base_cmd(), input=sql, text=True, env=env)
    if proc.returncode != 0:
        raise SystemExit(proc.returncode)


def make_duckdb_connection():
    """Create a DuckDB connection attached to this DuckLake.

    DuckDB is the local query engine. PostgreSQL stores DuckLake metadata. Azure
    Blob stores the actual Parquet files.
    """
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
    """Apply catalog RLS for the one-table model.

    The catalog RLS goal is now deliberately narrow: every non-admin demo role
    may discover the one canonical DuckLake table, `customers`, and nothing else.

    Region filtering and SSN masking are not encoded as more physical DuckLake
    tables. demo.py applies those policies in the service-layer SQL queries.
    """
    sql = """
-- Existing DuckLake catalog tables need grants for the demo roles. init.sql
-- sets default privileges for future tables; this handles tables already made.
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO
    ducklake_admin, ducklake_eu_analyst, ducklake_us_analyst, ducklake_pii_reader,
    ducklake_eu_limited, ducklake_us_limited;

-- Keep the older POC's region column if it already exists, but repurpose it as
-- simple catalog metadata for the one canonical table.
ALTER TABLE public.ducklake_table ADD COLUMN IF NOT EXISTS region VARCHAR DEFAULT 'global';
UPDATE public.ducklake_table SET region = 'all' WHERE table_name = 'customers';

ALTER TABLE public.ducklake_table ENABLE ROW LEVEL SECURITY;

-- Remove policies from earlier POC versions and recreate the current policy.
DROP POLICY IF EXISTS allow_ducklake_demo_roles ON public.ducklake_table;
DROP POLICY IF EXISTS allow_customer_table ON public.ducklake_table;
DROP POLICY IF EXISTS region_eu ON public.ducklake_table;
DROP POLICY IF EXISTS region_us ON public.ducklake_table;
DROP POLICY IF EXISTS region_masked ON public.ducklake_table;
DROP POLICY IF EXISTS "eu-limited" ON public.ducklake_table;
DROP POLICY IF EXISTS "us-limited" ON public.ducklake_table;

-- One-table catalog policy: demo roles can discover only the canonical customer
-- table. Historical versions of `customers` are also visible because DuckLake may
-- need snapshot metadata while querying. The demo display filters to the current
-- version with end_snapshot IS NULL.
CREATE POLICY allow_customer_table ON public.ducklake_table
    AS PERMISSIVE
    FOR SELECT
    TO ducklake_eu_analyst, ducklake_us_analyst, ducklake_pii_reader,
       ducklake_eu_limited, ducklake_us_limited
    USING (table_name = 'customers');
"""
    run_catalog_sql(sql)


def main():
    print("Attaching DuckLake as catalog admin (" + PG_ADMIN_USER + ")...")
    con = make_duckdb_connection()

    print("Creating single canonical customers table...")

    # Clean up tables from earlier versions of the POC. Dropping them marks their
    # catalog rows as ended; the current catalog view below hides them with
    # end_snapshot IS NULL.
    con.execute("DROP TABLE IF EXISTS customers_eu")
    con.execute("DROP TABLE IF EXISTS customers_us")
    con.execute("DROP TABLE IF EXISTS customers_masked")

    # This is the only physical customer table in the scalable model. Region
    # filtering and SSN masking are applied later by demo.py queries.
    con.execute("CREATE OR REPLACE TABLE customers AS "
                "SELECT * FROM (VALUES "
                "(1, 'Maria Schmidt', 'maria@example.eu', 'DE-123456789', 'eu'),"
                "(2, 'Lars Eriksen',  'lars@example.eu',  'NO-987654321', 'eu'),"
                "(3, 'John Smith',    'john@example.us',  'US-555-44-3333', 'us'),"
                "(4, 'Emily Johnson', 'emily@example.us', 'US-111-22-3333', 'us')"
                ") AS t(id, name, email, ssn, region)")

    # Force tiny demo rows out to Parquet files in Azure Blob. Otherwise DuckLake
    # can inline very small data in the catalog for efficiency.
    con.execute("CALL ducklake_flush_inlined_data('lake')")

    print("Applying Postgres RLS policies to ducklake_table...")
    apply_catalog_rls()

    print("\nCurrent catalog contents as admin:")
    for row in con.execute("SELECT table_name, region FROM __ducklake_metadata_lake.ducklake_table WHERE end_snapshot IS NULL ORDER BY table_name").fetchall():
        print("  " + str(row))

    print("\nParquet files materialized in Azure Blob:")
    for row in con.execute("SELECT path, file_size_bytes FROM __ducklake_metadata_lake.ducklake_data_file ORDER BY path").fetchall():
        print("  " + str(row[0]) + "  (" + str(row[1]) + " bytes)")

    print("\nSeed complete. Edit AS_USER in scripts/demo.py and run: python scripts/demo.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
