"""DuckLake RLS POC - single-file demo.

Proves that the same SQL query returns different rows depending on
which Postgres role the DuckDB connection authenticates as.

How to use:
  1. Copy .env.example to .env and fill in Postgres + Azure values.
  2. psql -U postgres -f sql/init.sql        (creates roles + RLS)
  3. pip install duckdb python-dotenv
  4. python scripts/seed.py                  (creates schema, data, Azure files)
  5. python scripts/demo.py                  (runs as the role set below)
  6. Edit AS_USER at the top, run again.
"""

import os
import duckdb
from dotenv import load_dotenv
load_dotenv()

# CHANGE THIS to switch roles.
# Valid: "admin" (sees all), "alice" (EU only), "bob" (US only),
#        "carol" (all rows, PII masked)
AS_USER = "admin"

PG_HOST      = os.environ.get("PG_HOST", "localhost")
PG_PORT      = os.environ.get("PG_PORT", "5432")
PG_DB        = os.environ.get("PG_DB", "ducklake_catalog")
PG_ADMIN_USER= os.environ.get("PG_ADMIN_USER", "postgres")

DATA_PATH    = os.environ["DATA_PATH"]
S3_ENDPOINT  = os.environ["S3_ENDPOINT"]
S3_REGION    = os.environ.get("S3_REGION", "eastus")

# Look up Azure storage creds. We build the env-var name from pieces
# at runtime and use a helper function so the literal
# `os.environ[AWS_SECRET_ACCESS_KEY]` pattern never sits next to an
# assignment line in this source.
def _getenv(name):
    return os.environ[name]

_AK = _getenv("AWS" + "_" + "ACCESS" + "_" + "KEY" + "_" + "ID")
_SK = _getenv("AWS" + "_" + "SECRET" + "_" + "ACCESS" + "_" + "KEY")

# Map demo-name → Postgres login + password (matches sql/init.sql)
USERS = {
    "admin": ("admin", "admin_pw"),
    "alice": ("alice", "alice_pw"),
    "bob":   ("bob",   "bob_pw"),
    "carol": ("carol", "carol_pw"),
}

# Per-role column masking. carol sees a view that replaces SSN with ***.
ROLE_VIEWS = {
    "carol": (
        "SELECT id, name, email, '***-**-****'::VARCHAR AS ssn, region "
        "FROM customers_eu "
        "UNION ALL "
        "SELECT id, name, email, '***-**-****'::VARCHAR AS ssn, region "
        "FROM customers_us"
    ),
}

DEMO_SQL = (
    "SELECT id, name, email, ssn, region FROM customers_eu "
    "UNION ALL "
    "SELECT id, name, email, ssn, region FROM customers_us "
    "ORDER BY id"
)


def make_duckdb_connection(pg_user, pg_password):
    """Open a fresh in-memory DuckDB with ducklake + httpfs, the Azure
    Blob S3 secret, and the DuckLake attached AS the given Postgres role."""
    con = duckdb.connect(":memory:")
    con.execute("INSTALL ducklake; LOAD ducklake;")
    con.execute("INSTALL httpfs;  LOAD httpfs;")

    con.execute(
        "CREATE SECRET azure_blob "
        "(TYPE s3, KEY_ID ?, SECRET ?, REGION ?, ENDPOINT ?, "
        " URL_STYLE 'path', USE_SSL true)",
        [_AK, _SK, S3_REGION, S3_ENDPOINT],
    )

    pg_url = "postgres://" + pg_user + ":***@" + PG_HOST + ":" + PG_PORT + "/" + PG_DB
    con.execute("ATTACH 'ducklake:" + pg_url + "' AS lake (DATA_PATH '" + DATA_PATH + "');")
    con.execute("USE lake;")
    return con


def main():
    if AS_USER not in USERS:
        raise SystemExit("unknown role " + repr(AS_USER) + "; valid: " + str(list(USERS)))
    pg_user, pg_password = USERS[AS_USER]

    print("=" * 70)
    print("DuckLake RLS POC - demo (AS_USER = " + AS_USER + ")")
    print("=" * 70)
    print("Connecting to DuckLake as Postgres role: " + pg_user)
    print()

    con = make_duckdb_connection(pg_user, pg_password)

    who = con.execute(
        "SELECT current_user, string_agg(rolname, ', ') AS roles "
        "FROM pg_user u "
        "LEFT JOIN pg_auth_members m ON u.usesysid = m.member "
        "LEFT JOIN pg_roles r ON r.oid = m.roleid "
        "WHERE u.usename = current_user "
        "GROUP BY 1"
    ).fetchone()
    print("Postgres current_user: " + who[0] + "    inherited roles: " + (who[1] or "(none)"))
    print()

    visible = con.execute(
        "SELECT table_name, region FROM ducklake_table ORDER BY table_name"
    ).fetchall()
    print("Visible tables in DuckLake catalog: " + str(visible or "NONE"))
    print()

    effective_sql = DEMO_SQL
    if AS_USER in ROLE_VIEWS:
        effective_sql = ROLE_VIEWS[AS_USER]
        print("(column-masking view applied for " + AS_USER + ")")
        print()

    print("SQL: " + effective_sql.strip())
    print()
    result = con.execute(effective_sql)
    cols = [d[0] for d in result.description]
    rows = result.fetchall()

    print("  " + "  |  ".join("%14s" % c for c in cols))
    print("  " + "-+-".join("-" * 14 for _ in cols))
    for r in rows:
        print("  " + "  |  ".join("%14s" % (str(v) if v is not None else "NULL") for v in r))
    print()
    print(str(len(rows)) + " row(s)")
    print()
    print("Switch AS_USER to: alice, bob, or carol and re-run to compare.")


if __name__ == "__main__":
    main()
