"""DuckLake RLS POC - seed script.

This file is the "build the demo lake" script. It does three jobs:

1. Connect DuckDB to a DuckLake catalog stored in PostgreSQL.
2. Create demo DuckLake tables and write their data files to Azure Blob.
3. Apply PostgreSQL Row-Level Security (RLS) to DuckLake's catalog table.

The key idea in this POC:

    PostgreSQL is the control plane.
    Azure Blob is the data plane.

DuckLake stores metadata such as table names, columns, snapshots, and Parquet
file paths in PostgreSQL tables named `ducklake_*`. The actual customer rows
are stored as Parquet files in Azure Blob. By putting RLS on PostgreSQL's
`ducklake_table`, we control which DuckLake tables a given login can discover.

Ordering matters:
  1. sql/init.sql creates the database, roles, and demo login users.
  2. This script ATTACHes DuckLake. That creates/populates ducklake_* catalog
     tables in PostgreSQL if they do not exist yet.
  3. This script adds our custom `region` metadata column to ducklake_table and
     creates RLS policies that filter visible catalog rows per role.
"""

import os
import shutil
import subprocess
import sys
from urllib.parse import quote

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
# e.g. az://ducklake-one/. DuckDB's azure extension handles the actual HTTPS
# calls to Azure behind the scenes.
DATA_PATH = _env("DATA_PATH")

# Azure Blob credentials used by DuckDB's Azure filesystem extension.
_ACCOUNT = _env(_env_name("AZURE", "STORAGE", "ACCOUNT"))
_TOKEN = _env(_env_name("AZURE", "STORAGE", "KEY"))


def pg_conninfo(user, pw=""):
    """Return a libpq-style connection string for DuckLake's Postgres catalog.

    DuckLake expects the PostgreSQL catalog connection in this format:

        host=localhost port=5432 dbname=ducklake_catalog user=admin password=...

    We omit `password=` for local superusers that use trust/peer-style auth.
    """
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
    """Base psql command used for catalog-side SQL changes.

    DuckDB can query the DuckLake catalog, but PostgreSQL RLS policies are normal
    PostgreSQL DDL. Running them through psql is simple and explicit.
    """
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

    This is the bridge between the three systems:

    - DuckDB is the query engine running in this Python process.
    - PostgreSQL stores the DuckLake catalog metadata.
    - Azure Blob stores the actual Parquet data files.

    After ATTACH, `lake` behaves like a DuckDB schema/database. Creating tables
    inside `lake` updates the Postgres catalog and writes files under DATA_PATH.
    """
    con = duckdb.connect(":memory:")

    # `ducklake` teaches DuckDB the DuckLake table format. `azure` teaches DuckDB
    # how to read/write az:// paths in Azure Blob Storage.
    con.execute("INSTALL ducklake; LOAD ducklake;")
    con.execute("INSTALL azure; LOAD azure;")

    # Give DuckDB credentials for Azure Blob. The key stays in .env and is not
    # printed. DuckDB uses this connection string whenever it accesses az://...
    con.execute(
        "SET azure_storage_connection_string = ?",
        ["DefaultEndpointsProtocol=https;AccountName=" + _ACCOUNT + ";AccountKey=" + _TOKEN + ";EndpointSuffix=core.windows.net"],
    )

    # ATTACH creates/connects the DuckLake named `lake`.
    #
    # The prefix `ducklake:postgres:` means: use PostgreSQL as the DuckLake
    # catalog backend. DATA_PATH tells DuckLake where to put data files.
    con.execute("ATTACH 'ducklake:postgres:" + pg_conninfo(PG_ADMIN_USER, _PW) + "' AS lake (DATA_PATH '" + DATA_PATH + "');")
    con.execute("USE lake;")
    return con


def apply_catalog_rls():
    """Add demo-specific RLS metadata and policies to DuckLake's catalog.

    DuckLake creates `public.ducklake_table` as an internal metadata table in
    PostgreSQL. Each logical DuckLake table has rows in this catalog table.

    We add a custom column named `region` to that catalog table. This is not a
    customer-data column. It is governance metadata used only for access control:

        customers_eu      -> region = 'eu'
        customers_us      -> region = 'us'
        customers_masked  -> region = 'masked'

    PostgreSQL RLS then filters catalog visibility:

        alice       can discover only customers_eu
        bob         can discover only customers_us
        carol       can discover only customers_masked
        eu-limited  can discover only customers_masked, then demo.py filters EU
        us-limited  can discover only customers_masked, then demo.py filters US

    The limited users intentionally see the masked projection at the catalog
    level. Their EU/US split is enforced by the service/demo query in demo.py.
    """
    sql = """
-- Existing DuckLake catalog tables need grants for the demo roles. init.sql
-- sets default privileges for future tables; this handles tables already made.
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO
    ducklake_admin, ducklake_eu_analyst, ducklake_us_analyst, ducklake_pii_reader,
    ducklake_eu_limited, ducklake_us_limited;

-- Add governance metadata to DuckLake's table catalog. DuckLake itself does not
-- require this column; it is POC-specific metadata for RLS policies.
ALTER TABLE public.ducklake_table ADD COLUMN IF NOT EXISTS region VARCHAR DEFAULT 'global';

-- Tag catalog rows by logical table. DuckLake keeps old table versions too, so
-- these UPDATEs may affect historical rows; the demo SELECTs later use
-- end_snapshot IS NULL to show only the current table versions.
UPDATE public.ducklake_table SET region = 'eu' WHERE table_name = 'customers_eu';
UPDATE public.ducklake_table SET region = 'us' WHERE table_name = 'customers_us';
UPDATE public.ducklake_table SET region = 'masked' WHERE table_name = 'customers_masked';

-- Turn on PostgreSQL Row-Level Security for the catalog table.
ALTER TABLE public.ducklake_table ENABLE ROW LEVEL SECURITY;

-- Recreate policies every seed run so this script is idempotent while the demo
-- is being iterated on.
DROP POLICY IF EXISTS allow_ducklake_demo_roles ON public.ducklake_table;
DROP POLICY IF EXISTS region_eu ON public.ducklake_table;
DROP POLICY IF EXISTS region_us ON public.ducklake_table;
DROP POLICY IF EXISTS region_masked ON public.ducklake_table;
DROP POLICY IF EXISTS "eu-limited" ON public.ducklake_table;
DROP POLICY IF EXISTS "us-limited" ON public.ducklake_table;

-- Postgres combines permissive policies with OR, then restrictive policies
-- with AND. Without at least one permissive policy, restrictive-only setup
-- defaults to deny-all.
--
-- This broad permissive policy says: the demo roles are allowed to SELECT from
-- ducklake_table at all. The restrictive policies below then narrow each role
-- down to only the rows it should see.
CREATE POLICY allow_ducklake_demo_roles ON public.ducklake_table
    AS PERMISSIVE
    FOR SELECT
    TO ducklake_eu_analyst, ducklake_us_analyst, ducklake_pii_reader,
       ducklake_eu_limited, ducklake_us_limited
    USING (true);

-- Alice's group can discover only the raw EU table.
CREATE POLICY region_eu ON public.ducklake_table
    AS RESTRICTIVE
    FOR SELECT
    TO ducklake_eu_analyst
    USING (region = 'eu');

-- Bob's group can discover only the raw US table.
CREATE POLICY region_us ON public.ducklake_table
    AS RESTRICTIVE
    FOR SELECT
    TO ducklake_us_analyst
    USING (region = 'us');

-- Carol's group can discover only the fully masked table.
CREATE POLICY region_masked ON public.ducklake_table
    AS RESTRICTIVE
    FOR SELECT
    TO ducklake_pii_reader
    USING (region = 'masked');

-- Limited regional roles also discover only the masked table. They do not get
-- direct catalog visibility into customers_eu/customers_us because those tables
-- contain raw SSNs. demo.py then adds WHERE region = 'eu' or 'us' on the masked
-- table to keep the regional restriction.
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

    # These CREATE TABLE statements create DuckLake tables, not PostgreSQL
    # tables. DuckDB records their metadata in the Postgres catalog and writes
    # their data to Azure Blob under DATA_PATH.
    #
    # customers_eu/customers_us are the raw regional tables.
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

    # customers_masked is a projection with the same rows but masked SSNs. The
    # carol/eu-limited/us-limited personas are only allowed to discover this
    # masked table through catalog RLS.
    con.execute("CREATE OR REPLACE TABLE customers_masked AS "
                "SELECT * FROM (VALUES "
                "(1, 'Maria Schmidt', 'maria@example.eu', '***-**-****', 'eu'),"
                "(2, 'Lars Eriksen',  'lars@example.eu',  '***-**-****', 'eu'),"
                "(3, 'John Smith',    'john@example.us',  '***-**-****', 'us'),"
                "(4, 'Emily Johnson', 'emily@example.us', '***-**-****', 'us')"
                ") AS t(id, name, email, ssn, region)")

    # The demo is explicitly about Parquet files in object storage. DuckLake can
    # inline tiny inserts inside the catalog for efficiency; this call forces the
    # tiny demo data out to actual Parquet files in Azure Blob so the storage
    # layout is visible.
    con.execute("CALL ducklake_flush_inlined_data('lake')")

    print("Applying Postgres RLS policies to ducklake_table...")
    apply_catalog_rls()

    print("\nCatalog contents as admin:")

    # DuckLake tracks table history. `end_snapshot IS NULL` means "current table
    # version only". Without it, repeated seed runs show older versions too.
    for row in con.execute("SELECT table_name, region FROM __ducklake_metadata_lake.ducklake_table WHERE end_snapshot IS NULL ORDER BY table_name").fetchall():
        print("  " + str(row))

    print("\nParquet files materialized in Azure Blob:")
    for row in con.execute("SELECT path, file_size_bytes FROM __ducklake_metadata_lake.ducklake_data_file ORDER BY path").fetchall():
        print("  " + str(row[0]) + "  (" + str(row[1]) + " bytes)")

    print("\nSeed complete. Edit AS_USER in scripts/demo.py and run: python scripts/demo.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
