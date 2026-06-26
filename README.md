# DuckLake + PostgreSQL RLS + Column Masking POC

A proof-of-concept demonstrating:

- **DuckLake v1.0** as the open table format (Parquet data + catalog in PostgreSQL)
- **Azure Blob Storage** (via the S3-compatible endpoint) as the data path
- **PostgreSQL Row-Level Security** policies on the DuckLake catalog tables
- **Per-role column masking** via persona views
- A **single Python script** (`scripts/demo.py`) that proves the pattern by changing one variable

## What this proves

A single SQL query — same string every time — returns **different rows and different column values** when run as different roles, because:

- RLS policies on `ducklake_table` filter which Parquet files each role can see
- A persona view (`customers_masked`) hides the `ssn` column for the PII-reader role
- The Parquet files in Azure Blob never change

The demo script runs the **same query** four times, with one variable changed:

```python
AS_USER = "admin"     # sees everything: EU + US rows, all columns
AS_USER = "alice"     # ducklake_eu_analyst: only EU rows
AS_USER = "bob"       # ducklake_us_analyst: only US rows
AS_USER = "carol"     # ducklake_pii_reader: all rows, but ssn = ***-**-****
```

No per-role code paths, no separate SQL strings per role. RLS does the work.

## Prerequisites

You need:

1. **PostgreSQL 12+** running locally (or anywhere reachable from your machine)
2. **Python 3.10+** with `pip install duckdb python-dotenv`
3. **An Azure storage account** with a blob container created (any name)

## Quickstart

### 1. Configure the POC

```bash
cd ducklake-rls-poc
cp .env.example .env
# Edit .env with your values (see below)
```

The `.env` file needs:
- `PG_HOST` — where your Postgres is reachable (default: `localhost`)
- `PG_PORT` (default: 5432), `PG_DB` (default: `ducklake_catalog`)
- `PG_ADMIN_USER` (default: `postgres`) and optionally `PG_ADMIN_PW`
- `AZURE_STORAGE_ACCOUNT` and `AZURE_STORAGE_KEY` — from Azure portal → Storage Account → Access keys
- `AZURE_CONTAINER` — the blob container name you created
- `S3_ENDPOINT` — `https://<account>.blob.core.windows.net`
- `S3_REGION` (default: `eastus`)
- `DATA_PATH` — `s3://<container>/`

### 2. Initialize Postgres

Run the bootstrap SQL as a Postgres superuser:

```bash
psql -U postgres -f sql/init.sql
```

This creates:
- The `ducklake_catalog` database
- Four group roles: `ducklake_admin`, `ducklake_eu_analyst`, `ducklake_us_analyst`, `ducklake_pii_reader`
- Four login users: `admin`, `alice`, `bob`, `carol` (with the dev passwords shown in the file)
- RLS policies on `ducklake_table` (pre-created, activated by the seed script)
- Default privileges so future catalog tables are accessible to all roles

### 3. Seed the DuckLake

```bash
python scripts/seed.py
```

This:
- Connects to Postgres as `PG_ADMIN_USER` (defaults to `postgres`)
- Attaches the DuckLake pointing at your Azure Blob container
- Adds the `region` column to `ducklake_table`
- Enables RLS on `ducklake_table`
- Creates two tables (`customers_eu`, `customers_us`) with sample data
- Tags each table with its region
- Creates the `customers_masked` view for PII role
- **Materialises Parquet files in your Azure Blob container** — you should see them appear in the portal

### 4. Run the demo as each role

Edit `scripts/demo.py` and change `AS_USER`:

```python
AS_USER = "admin"     # sees everything
python scripts/demo.py

# Set AS_USER = "alice" and re-run
python scripts/demo.py

# Set AS_USER = "bob" and re-run
python scripts/demo.py

# Set AS_USER = "carol" and re-run
python scripts/demo.py
```

Each run prints:
- The Postgres user + role memberships
- The tables visible in the catalog (RLS-filtered)
- The query results — different row counts and SSN values per role

## Roles in this POC

| Demo name | Postgres login | Role | Sees |
|---|---|---|---|
| `admin` | `admin` / `admin_pw` | `ducklake_admin` (BYPASSRLS) | All rows, all columns, all tables |
| `alice` | `alice` / `alice_pw` | `ducklake_eu_analyst` | Only tables tagged `region = 'eu'` |
| `bob` | `bob` / `bob_pw` | `ducklake_us_analyst` | Only tables tagged `region = 'us'` |
| `carol` | `carol` / `carol_pw` | `ducklake_pii_reader` | All rows (no RLS), but `ssn` masked via view |

The dev passwords in `sql/init.sql` are a POC convenience. In production: pull from a secret store.

## What this POC does NOT cover

- **No production auth.** The `AS_USER` variable is the entire auth model.
- **No audit log.** A few SQL triggers away.
- **No multi-engine governance.** Only DuckDB is enforced. Polars / PyArrow / Spark pointing at the same Azure files would bypass RLS entirely.
- **No encryption at rest** beyond what Azure Blob provides by default.
- **No automated migration path** from DuckDB → DuckLake. The seed script just inserts rows.

## File layout

```
ducklake-rls-poc/
├── .env.example           # template for Postgres + Azure config
├── .gitignore
├── README.md              # this file
├── sql/
│   └── init.sql           # creates roles, login users, RLS policies
└── scripts/
    ├── seed.py            # one-time bootstrap: schema, RLS, data, Azure files
    └── demo.py            # the demo: change AS_USER, run, observe
```

## Why "PostgreSQL + RLS" and not "Unity Catalog"?

Honest answer: it's not as good as Unity Catalog. Unity Catalog enforces policy across every engine (Spark, Polars, notebooks, BI). This POC enforces policy only on the DuckDB query path. The moment someone reads the Parquet files directly with a different tool, RLS is bypassed.

For a one-person or small-team project, layer 1 (RLS in the catalog) is sufficient. The path toward Unity-Catalog-equivalent governance is documented in the `ducklake-expertise` skill.

## Troubleshooting

- **`role "ducklake_admin" does not exist`** — re-run `sql/init.sql`; it uses `DO $$ BEGIN ... EXCEPTION WHEN duplicate_object` so it's idempotent.
- **`permission denied for table ducklake_table`** — the demo/seed user doesn't have the right role. Check `FROM pg_user;` and the role memberships.
- **`IO Error: Could not connect to ... blob.core.windows.net`** — wrong `S3_ENDPOINT` or `AZURE_STORAGE_KEY`. Test with `az storage blob list` from your terminal.
- **The demo shows all 4 rows for everyone** — RLS isn't on. Check `SELECT relname, relrowsecurity FROM pg_class WHERE relname = 'ducklake_table';` — `relrowsecurity` should be `t`.
- **`extension "ducklake" is not installed`** — DuckDB's ducklake extension isn't in the official binary yet. Use DuckDB v1.5.2+ from `pip install duckdb`; the extension is bundled.
