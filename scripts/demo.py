"""DuckLake RLS POC - single-file demo.

This file is the "pretend application" layer for the POC.

seed.py builds the lake and configures catalog RLS. demo.py shows what happens
when different application users connect through different PostgreSQL roles.

The important split:

- PostgreSQL RLS controls which DuckLake catalog rows a role can see.
- The query chosen by this script controls which safe table/projection the
  application reads from.

That is why the limited personas work like this:

- eu-limited can discover only `customers_masked` in the DuckLake catalog.
- demo.py then queries `customers_masked WHERE region = 'eu'`.
- Result: EU rows only, with SSNs already masked.

Same idea for us-limited.
"""

import os
from urllib.parse import quote

import duckdb
from dotenv import load_dotenv

# Load local config from .env. This provides PostgreSQL, Azure account, Azure
# key, and DATA_PATH settings without hardcoding secrets in the script.
load_dotenv()

# CHANGE THIS to switch roles.
#
# Valid values:
#   "admin"      -> sees raw EU + raw US rows
#   "alice"      -> sees raw EU rows only
#   "bob"        -> sees raw US rows only
#   "carol"      -> sees all rows, but only through the masked table
#   "eu-limited" -> sees EU rows only, through the masked table
#   "us-limited" -> sees US rows only, through the masked table
AS_USER = "admin"


def _env(name, default=None):
    """Read a required environment variable with a clear error message."""
    value = os.environ.get(name, default)
    if value is None or value == "":
        raise SystemExit("Missing required environment variable: " + name)
    return value


def _env_name(*parts):
    """Build environment variable names like AZURE_STORAGE_ACCOUNT."""
    return "_".join(parts)


# PostgreSQL catalog connection settings. This database stores DuckLake metadata
# tables such as ducklake_table and ducklake_data_file.
PG_HOST = os.environ.get("PG_HOST", "localhost")
PG_PORT = os.environ.get("PG_PORT", "5432")
PG_DB = os.environ.get("PG_DB", "ducklake_catalog")

# Azure Blob data path. This is where DuckLake's Parquet files live.
DATA_PATH = _env("DATA_PATH")

# Azure credentials for DuckDB's azure extension. The key is read from .env and
# never printed.
_ACCOUNT = _env(_env_name("AZURE", "STORAGE", "ACCOUNT"))
_TOKEN = _env(_env_name("AZURE", "STORAGE", "KEY"))

# Application persona map.
#
# Each entry is:
#   AS_USER value -> (Postgres login, Postgres password, query to run)
#
# The Postgres login is important because DuckLake uses it to read the catalog.
# If the login is alice, PostgreSQL RLS filters ducklake_table so DuckDB can only
# discover the catalog rows alice is allowed to see.
#
# The query is the service/application routing decision. For raw analysts it
# chooses the raw regional table. For masked/limited users it chooses the masked
# projection.
USERS = {
    "admin": (
        "admin",
        "admin_pw",
        "SELECT id, name, email, ssn, region "
        "FROM customers_eu "
        "UNION ALL "
        "SELECT id, name, email, ssn, region "
        "FROM customers_us "
        "ORDER BY id",
    ),
    "alice": (
        "alice",
        "alice_pw",
        "SELECT id, name, email, ssn, region FROM customers_eu ORDER BY id",
    ),
    "bob": (
        "bob",
        "bob_pw",
        "SELECT id, name, email, ssn, region FROM customers_us ORDER BY id",
    ),
    "carol": (
        "carol",
        "carol_pw",
        "SELECT id, name, email, ssn, region FROM customers_masked ORDER BY id",
    ),
    "eu-limited": (
        "eu_limited",
        "eu_limited_pw",
        "SELECT id, name, email, ssn, region "
        "FROM customers_masked "
        "WHERE region = 'eu' "
        "ORDER BY id",
    ),
    "us-limited": (
        "us_limited",
        "us_limited_pw",
        "SELECT id, name, email, ssn, region "
        "FROM customers_masked "
        "WHERE region = 'us' "
        "ORDER BY id",
    ),
}


def pg_conninfo(user, pw):
    """Build the libpq connection string used inside DuckLake's ATTACH."""
    return " ".join([
        "host=" + PG_HOST,
        "port=" + PG_PORT,
        "dbname=" + PG_DB,
        "user=" + user,
        "password=" + pw,
    ])


def make_duckdb_connection(pg_user, pw):
    """Create DuckDB connection as one specific demo persona.

    This function is where identity becomes authorization:

    1. DuckDB loads DuckLake and Azure support.
    2. DuckDB receives Azure credentials so it can read Parquet files.
    3. DuckDB ATTACHes the DuckLake using the selected PostgreSQL login.

    Because ATTACH uses that login, PostgreSQL RLS applies when DuckDB reads the
    DuckLake catalog. Different users therefore discover different DuckLake
    tables before any query runs.
    """
    con = duckdb.connect(":memory:")

    # `ducklake` = table format/catalog integration.
    # `azure` = support for reading/writing az:// object storage paths.
    con.execute("INSTALL ducklake; LOAD ducklake;")
    con.execute("INSTALL azure; LOAD azure;")

    # Let DuckDB access Azure Blob data files. The Azure account key remains in
    # .env; this code passes it directly to DuckDB without printing it.
    con.execute(
        "SET azure_storage_connection_string = ?",
        ["DefaultEndpointsProtocol=https;AccountName=" + _ACCOUNT + ";AccountKey=" + _TOKEN + ";EndpointSuffix=core.windows.net"],
    )

    # Attach the DuckLake catalog using the persona's PostgreSQL login.
    #
    # Example expansion:
    #   ATTACH 'ducklake:postgres:host=localhost port=5432 dbname=ducklake_catalog user=alice password=alice_pw'
    #       AS lake (DATA_PATH 'az://ducklake-one/');
    #
    # `USE lake` makes subsequent SQL queries refer to DuckLake tables by default.
    con.execute("ATTACH 'ducklake:postgres:" + pg_conninfo(pg_user, pw) + "' AS lake (DATA_PATH '" + DATA_PATH + "');")
    con.execute("USE lake;")
    return con


def print_rows(rows, cols):
    """Pretty-print query results in a small terminal table."""
    print("  " + "  |  ".join("%14s" % c for c in cols))
    print("  " + "-+-".join("-" * 14 for _ in cols))
    for row in rows:
        print("  " + "  |  ".join("%14s" % (str(v) if v is not None else "NULL") for v in row))


def main():
    if AS_USER not in USERS:
        raise SystemExit("unknown AS_USER " + repr(AS_USER) + "; valid: " + ", ".join(USERS))

    # Resolve the selected persona into database credentials and the query that
    # the application is allowed to run for that persona.
    pg_user, pw, sql = USERS[AS_USER]

    print("=" * 70)
    print("DuckLake RLS POC - AS_USER = " + AS_USER)
    print("=" * 70)
    con = make_duckdb_connection(pg_user, pw)

    # This query reads DuckLake's metadata view. It proves PostgreSQL RLS is
    # filtering catalog visibility. If alice sees only customers_eu here, DuckDB
    # cannot discover customers_us through this attached catalog connection.
    #
    # end_snapshot IS NULL means "current table version only". DuckLake keeps
    # history, and repeated seed runs leave older table versions in the catalog.
    visible = con.execute("SELECT table_name, region FROM __ducklake_metadata_lake.ducklake_table WHERE end_snapshot IS NULL ORDER BY table_name").fetchall()
    print("Visible DuckLake catalog tables after Postgres RLS:")
    print("  " + str(visible or "NONE"))
    print()

    # This is the application routing part. RLS controls what tables are visible;
    # the service layer still chooses the safe query for the authenticated user.
    print("Query routed by persona:")
    print("  " + sql)
    print()

    result = con.execute(sql)
    cols = [d[0] for d in result.description]
    rows = result.fetchall()
    print_rows(rows, cols)
    print()
    print(str(len(rows)) + " row(s)")
    print("Expected: admin=4 raw rows, alice=2 EU rows, bob=2 US rows, carol=4 masked rows, eu-limited=2 masked EU rows, us-limited=2 masked US rows.")


if __name__ == "__main__":
    main()
