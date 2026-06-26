"""DuckLake RLS POC - single-file demo.

Change AS_USER below and rerun this file to demonstrate:
  - Postgres RLS on the DuckLake catalog (visible tables differ by role)
  - column masking through the masked projection used for carol
"""

import os
from urllib.parse import quote

import duckdb
from dotenv import load_dotenv

load_dotenv()

# CHANGE THIS to switch roles.
# Valid: "admin" (sees all raw rows), "alice" (EU only),
#        "bob" (US only), "carol" (all rows, SSN masked)
AS_USER = "admin"


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

DATA_PATH = _env("DATA_PATH")
S3_ENDPOINT = _env("S3_ENDPOINT")
S3_REGION = os.environ.get("S3_REGION", "eastus")

_ACCOUNT = _env(_env_name("AZURE", "STORAGE", "ACCOUNT"))
_TOKEN = _env(_env_name("AZURE", "STORAGE", "KEY"))

USERS = {
    "admin": ("admin", "admin_pw", "SELECT id, name, email, ssn, region FROM customers_eu UNION ALL SELECT id, name, email, ssn, region FROM customers_us ORDER BY id"),
    "alice": ("alice", "alice_pw", "SELECT id, name, email, ssn, region FROM customers_eu ORDER BY id"),
    "bob":   ("bob", "bob_pw", "SELECT id, name, email, ssn, region FROM customers_us ORDER BY id"),
    "carol": ("carol", "carol_pw", "SELECT id, name, email, ssn, region FROM customers_masked ORDER BY id"),
}


def pg_conninfo(user, pw):
    return " ".join([
        "host=" + PG_HOST,
        "port=" + PG_PORT,
        "dbname=" + PG_DB,
        "user=" + user,
        "password=" + pw,
    ])


def make_duckdb_connection(pg_user, pw):
    con = duckdb.connect(":memory:")
    con.execute("INSTALL ducklake; LOAD ducklake;")
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute(
        "CREATE OR REPLACE SECRET azure_blob "
        "(TYPE s3, KEY_ID ?, SECRET ?, REGION ?, ENDPOINT ?, URL_STYLE 'path', USE_SSL true)",
        [_ACCOUNT, _TOKEN, S3_REGION, S3_ENDPOINT],
    )
    con.execute("ATTACH 'ducklake:postgres:" + pg_conninfo(pg_user, pw) + "' AS lake (DATA_PATH '" + DATA_PATH + "');")
    con.execute("USE lake;")
    return con


def print_rows(rows, cols):
    print("  " + "  |  ".join("%14s" % c for c in cols))
    print("  " + "-+-".join("-" * 14 for _ in cols))
    for row in rows:
        print("  " + "  |  ".join("%14s" % (str(v) if v is not None else "NULL") for v in row))


def main():
    if AS_USER not in USERS:
        raise SystemExit("unknown AS_USER " + repr(AS_USER) + "; valid: " + ", ".join(USERS))
    pg_user, pw, sql = USERS[AS_USER]
    print("=" * 70)
    print("DuckLake RLS POC - AS_USER = " + AS_USER)
    print("=" * 70)
    con = make_duckdb_connection(pg_user, pw)

    visible = con.execute("SELECT table_name, region FROM __ducklake_metadata_lake.ducklake_table ORDER BY table_name").fetchall()
    print("Visible DuckLake catalog tables after Postgres RLS:")
    print("  " + str(visible or "NONE"))
    print()
    print("Query routed by persona:")
    print("  " + sql)
    print()
    result = con.execute(sql)
    cols = [d[0] for d in result.description]
    rows = result.fetchall()
    print_rows(rows, cols)
    print()
    print(str(len(rows)) + " row(s)")
    print("Expected: admin=4 raw rows, alice=2 EU rows, bob=2 US rows, carol=4 masked rows.")


if __name__ == "__main__":
    main()
