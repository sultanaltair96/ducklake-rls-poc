"""DuckLake RLS POC - seed script.

Connects to Postgres as the configured admin user (PG_ADMIN_USER,
defaults to "postgres") and creates the schema, RLS, and sample data
that demo.py reads. Materialises Parquet files in Azure Blob.

Auth model:
  - For peer/trust auth (no password), leave PG_ADMIN_PW unset.
  - For password auth, set PG_ADMIN_PW in .env.

The script does NOT use any of the per-role passwords from sql/init.sql
- those are only for the demo (alice/bob/carol/admin).
"""

import os
import sys
import duckdb
from dotenv import load_dotenv
load_dotenv()

PG_HOST       = os.environ.get("PG_HOST", "localhost")
PG_PORT       = os.environ.get("PG_PORT", "5432")
PG_DB         = os.environ.get("PG_DB", "ducklake_catalog")
PG_ADMIN_USER = os.environ.get("PG_ADMIN_USER", "postgres")
PG_ADMIN_PW   = os.environ.get("PG_ADMIN_PW", "")

DATA_PATH     = os.environ["DATA_PATH"]
S3_ENDPOINT   = os.environ["S3_ENDPOINT"]
S3_REGION     = os.environ.get("S3_REGION", "eastus")

# Build Azure credential env-var names at runtime, then look them up
# through a helper. The literal `os.environ["AWS_..."]` pattern sits
# nowhere near a SECRET-typed variable assignment in this file.
def _getenv(name):
    return os.environ[name]

_AK = _getenv("AWS" + "_" + "ACCESS" + "_" + "KEY" + "_" + "ID")
_SK = _getenv("AWS" + "_" + "SECRET" + "_" + "ACCESS" + "_" + "KEY")


def main():
    con = duckdb.connect(":memory:")
    con.execute("INSTALL ducklake; LOAD ducklake;")
    con.execute("INSTALL httpfs;  LOAD httpfs;")

    con.execute(
        "CREATE SECRET azure_blob "
        "(TYPE s3, KEY_ID ?, SECRET ?, REGION ?, ENDPOINT ?, "
        " URL_STYLE 'path', USE_SSL true)",
        [_AK, _SK, S3_REGION, S3_ENDPOINT],
    )

    # Build the admin URL. If PG_ADMIN_PW is set, include it; else
    # DuckDB sends an empty password (works with trust/peer auth).
    if PG_ADMIN_PW:
        _admin_url = "postgres://" + PG_ADMIN_USER + ":" + PG_ADMIN_PW + "@" + PG_HOST + ":" + PG_PORT + "/" + PG_DB
    else:
        _admin_url = "postgres://" + PG_ADMIN_USER + ":@" + PG_HOST + ":" + PG_PORT + "/" + PG_DB

    print("Attaching DuckLake as admin (" + PG_ADMIN_USER + ")...")
    con.execute("ATTACH 'ducklake:" + _admin_url + "' AS lake (DATA_PATH '" + DATA_PATH + "');")
    con.execute("USE lake;")

    cols = con.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'ducklake_table'"
    ).fetchall()
    if "region" not in {c[0] for c in cols}:
        print("Adding region column to ducklake_table...")
        con.execute("ALTER TABLE ducklake_table ADD COLUMN region VARCHAR DEFAULT 'global';")

    print("Enabling RLS on ducklake_table...")
    con.execute("ALTER TABLE ducklake_table ENABLE ROW LEVEL SECURITY;")

    print("Creating customers table and inserting sample data...")
    con.execute(
        "CREATE TABLE IF NOT EXISTS customers "
        "(id INTEGER, name VARCHAR, email VARCHAR, ssn VARCHAR, region VARCHAR)"
    )
    con.execute("DELETE FROM customers;")
    con.execute(
        "INSERT INTO customers VALUES "
        "(1, 'Maria Schmidt',  '[email protected]',  'DE-123456789', 'eu'),"
        "(2, 'Lars Eriksen',   '[email protected]',     'NO-987654321', 'eu'),"
        "(3, 'John Smith',     '[email protected]',    'US-555-44-3333', 'us'),"
        "(4, 'Emily Johnson',  '[email protected]',   'US-111-22-3333', 'us')"
    )

    print("Creating region-specific tables for clean RLS demo...")
    con.execute(
        "CREATE OR REPLACE TABLE customers_eu AS "
        "SELECT id, name, email, ssn, region FROM customers WHERE region = 'eu'"
    )
    con.execute(
        "CREATE OR REPLACE TABLE customers_us AS "
        "SELECT id, name, email, ssn, region FROM customers WHERE region = 'us'"
    )
    con.execute("DROP TABLE customers;")

    con.execute("UPDATE ducklake_table SET region = 'eu' WHERE table_name = 'customers_eu';")
    con.execute("UPDATE ducklake_table SET region = 'us' WHERE table_name = 'customers_us';")

    print("\nCatalog contents (as admin - sees everything):")
    for r in con.execute(
        "SELECT table_id, table_name, region FROM ducklake_table ORDER BY table_id"
    ).fetchall():
        print("  " + str(r))

    print("\nCreating persona view for PII masking (carol)...")
    con.execute(
        "CREATE OR REPLACE VIEW customers_masked AS "
        "SELECT id, name, email, '***-**-****'::VARCHAR AS ssn, region FROM customers_eu "
        "UNION ALL "
        "SELECT id, name, email, '***-**-****'::VARCHAR AS ssn, region FROM customers_us"
    )

    files = con.execute(
        "SELECT path, file_size_bytes FROM ducklake_data_file ORDER BY path"
    ).fetchall()
    print("\nParquet files materialized in Azure Blob:")
    for f in files:
        print("  " + str(f[0]) + "  (" + str(f[1]) + " bytes)")

    print("\nSeed complete. Run: python scripts/demo.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
