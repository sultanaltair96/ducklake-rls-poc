# DuckLake + PostgreSQL RLS + Column Masking - Step Zero POC

This repo demonstrates a small but realistic governance pattern:

- DuckLake stores table metadata in PostgreSQL.
- DuckLake stores the actual data as Parquet files in Azure Blob via DuckDB's Azure filesystem extension.
- PostgreSQL Row-Level Security (RLS) controls which DuckLake catalog rows a role can see.
- A simple persona router (`AS_USER` in `scripts/demo.py`) shows different results for admin, EU analyst, US analyst, and a PII-masked reader.

This is deliberately not a web service, not Terraform, and not Docker. It is the smallest useful POC: SQL + two Python scripts.

---

## 0. What is DuckLake?

DuckLake is an open table format from the DuckDB team. It has two parts:

1. **Catalog** - normal SQL tables such as `ducklake_table`, `ducklake_data_file`, and `ducklake_snapshot`. In this POC the catalog lives in PostgreSQL.
2. **Data files** - immutable Parquet files. In this POC they live in Azure Blob Storage, addressed through an Azure URL such as `az://ducklake-data/`.

When DuckDB queries a DuckLake table, it first asks the catalog which table/data files exist. That makes the catalog a useful governance choke point.

## 0.5 What is RLS?

PostgreSQL Row-Level Security lets the database silently filter rows based on the current database role:

```sql
ALTER TABLE ducklake_table ENABLE ROW LEVEL SECURITY;

CREATE POLICY region_eu ON ducklake_table
    AS RESTRICTIVE
    FOR ALL
    TO ducklake_eu_analyst
    USING (region = 'eu')
    WITH CHECK (region = 'eu');
```

In DuckLake, filtering `ducklake_table` changes which DuckLake tables are visible to a role. That is how this POC demonstrates catalog-level access control.

## 0.6 What you will build

```
+--------------------------------------------------+
| scripts/demo.py                                  |
|   change AS_USER = admin / alice / bob / carol   |
+-------------------------+------------------------+
                          |
                          | ATTACH 'ducklake:postgres:host=... user=...'
                          v
+--------------------------------------------------+
| PostgreSQL catalog                               |
|   ducklake_table has extra column: region         |
|   RLS policies filter visible catalog rows        |
+-------------------------+------------------------+
                          |
                          | paths in ducklake_data_file
                          v
+--------------------------------------------------+
| Azure Blob Storage                               |
|   az://ducklake-data/main/customers_eu/*.parquet |
|   az://ducklake-data/main/customers_us/*.parquet |
+--------------------------------------------------+
```

The demo creates three DuckLake tables:

| Table | Region tag in catalog | Who sees it |
|---|---|---|
| `customers_eu` | `eu` | admin, alice |
| `customers_us` | `us` | admin, bob |
| `customers_masked` | `masked` | carol, eu-limited, us-limited |

`customers_masked` is the column-masked projection: same rows, but `ssn` is replaced with `***-**-****`.
The limited personas use the masked projection plus a service-layer region filter, so `eu-limited` sees masked EU rows and `us-limited` sees masked US rows.

---

## 1. Prerequisites

You need:

1. PostgreSQL with the `psql` CLI available.
2. Python 3.10+.
3. Python packages:
   ```bash
   pip install -r requirements.txt
   ```
4. An Azure Storage Account + Blob container.

Create a blob container named `ducklake-data` or change `AZURE_CONTAINER` / `DATA_PATH` in `.env`.

---

## 2. Configure `.env`

```bash
cd ducklake-rls-poc
cp .env.example .env
```

Edit `.env`:

```bash
PG_HOST=localhost
PG_PORT=5432
PG_DB=ducklake_catalog
PG_ADMIN_USER=postgres
PG_ADMIN_PASSWORD=postgres

AZURE_STORAGE_ACCOUNT=youraccountname
AZURE_STORAGE_KEY=your_storage_account_key
AZURE_CONTAINER=ducklake-data
DATA_PATH=az://ducklake-data/
```

`PG_ADMIN_PASSWORD` can be blank if your local Postgres uses peer/trust authentication.

---

## 3. Step 1 - initialize Postgres

Run:

```bash
psql -U postgres -f sql/init.sql
```

This creates:

- database: `ducklake_catalog`
- group roles: `ducklake_admin`, `ducklake_eu_analyst`, `ducklake_us_analyst`, `ducklake_pii_reader`, `ducklake_eu_limited`, `ducklake_us_limited`
- login users: `admin`, `alice`, `bob`, `carol`, `eu_limited`, `us_limited`
- default privileges for future DuckLake catalog tables

Important: `init.sql` does **not** create RLS policies yet. On a fresh database, `ducklake_table` does not exist until DuckLake is first attached. `scripts/seed.py` applies RLS after the catalog tables exist.

---

## 4. Step 2 - seed DuckLake and apply catalog RLS

Run:

```bash
python scripts/seed.py
```

What `seed.py` does:

1. Installs and loads DuckDB extensions: `ducklake` and `azure`.
2. Configures DuckDB's Azure filesystem extension using `AZURE_STORAGE_ACCOUNT` and `AZURE_STORAGE_KEY`.
3. Attaches the DuckLake as the Postgres admin user:
   ```sql
   ATTACH 'ducklake:postgres:host=localhost port=5432 dbname=ducklake_catalog user=postgres password=postgres'
       AS lake (DATA_PATH 'az://ducklake-data/');
   ```
4. Creates three DuckLake tables:
   - `customers_eu`
   - `customers_us`
   - `customers_masked`
5. Flushes the tiny demo rows out of DuckLake's inlined catalog storage and into Parquet:
   ```sql
   CALL ducklake_flush_inlined_data('lake');
   ```
6. Uses `psql` to update the PostgreSQL catalog:
   ```sql
   ALTER TABLE public.ducklake_table ADD COLUMN IF NOT EXISTS region VARCHAR DEFAULT 'global';
   UPDATE public.ducklake_table SET region = 'eu' WHERE table_name = 'customers_eu';
   UPDATE public.ducklake_table SET region = 'us' WHERE table_name = 'customers_us';
   UPDATE public.ducklake_table SET region = 'masked' WHERE table_name = 'customers_masked';
   ALTER TABLE public.ducklake_table ENABLE ROW LEVEL SECURITY;
   CREATE POLICY allow_ducklake_demo_roles ON public.ducklake_table ...;
   CREATE POLICY region_eu ON public.ducklake_table ...;
   CREATE POLICY region_us ON public.ducklake_table ...;
   CREATE POLICY region_masked ON public.ducklake_table ...;
   CREATE POLICY "eu-limited" ON public.ducklake_table ...;
   CREATE POLICY "us-limited" ON public.ducklake_table ...;
   ```

After this step you should see Parquet files in Azure Blob under paths like:

```text
az://ducklake-data/main/customers_eu/...
az://ducklake-data/main/customers_us/...
az://ducklake-data/main/customers_masked/...
```

---

## 5. Step 3 - demonstrate RLS and column masking

Open `scripts/demo.py` and change:

```python
AS_USER = "admin"
```

Valid values:

| `AS_USER` | Postgres login | Expected result |
|---|---|---|
| `admin` | `admin` | 4 raw rows from EU + US |
| `alice` | `alice` | 2 EU rows only |
| `bob` | `bob` | 2 US rows only |
| `carol` | `carol` | 4 rows with masked SSN |
| `eu-limited` | `eu_limited` | 2 EU rows with masked SSN |
| `us-limited` | `us_limited` | 2 US rows with masked SSN |

Run:

```bash
python scripts/demo.py
```

The script prints two things:

1. **Visible DuckLake catalog tables after Postgres RLS** - this proves RLS is active.
2. **The routed query result** - this shows the rows each persona gets.

This is intentionally similar to a tiny service layer: authenticated identity maps to a safe table/projection.

---

## 6. What just happened?

- Alice, Bob, and Carol connect to the **same DuckLake**.
- The data lives in the **same Azure Blob container**.
- The difference is the **Postgres role** used in the DuckLake `ATTACH` string.
- PostgreSQL RLS filters rows in `ducklake_table`, so each role sees a different catalog surface.
- Carol's column masking is shown through the `customers_masked` projection.

This is not a full Unity Catalog replacement. It is a minimal, working shape for catalog-controlled DuckLake access through one query surface.

---

## 7. Expected demo output shape

### `AS_USER = "admin"`

- Visible catalog tables: `customers_eu`, `customers_us`, `customers_masked`
- Result rows: 4
- SSNs: raw

### `AS_USER = "alice"`

- Visible catalog tables: `customers_eu`
- Result rows: 2
- Region: `eu`

### `AS_USER = "bob"`

- Visible catalog tables: `customers_us`
- Result rows: 2
- Region: `us`

### `AS_USER = "carol"`

- Visible catalog tables: `customers_masked`
- Result rows: 4
- SSNs: `***-**-****`

---

## 8. Troubleshooting

- **`psql not found`** - install PostgreSQL client tools. `seed.py` uses `psql` to apply catalog RLS after DuckLake creates the catalog tables.
- **`ducklake_table does not exist` during init** - you are using an old README/script. Current `init.sql` does not touch `ducklake_table`; `seed.py` does.
- **`Missing required environment variable: AZURE_STORAGE_KEY`** - fill `.env` from `.env.example`.
- **`IO Error ... blob.core.windows.net`** - check `AZURE_STORAGE_ACCOUNT`, `AZURE_STORAGE_KEY`, and `DATA_PATH`.
- **Alice/Bob/Carol see no catalog tables** - rerun `python scripts/seed.py`; the RLS policies and region tags are applied there.

---

## File layout

```text
ducklake-rls-poc/
├── .env.example
├── README.md
├── sql/
│   └── init.sql
└── scripts/
    ├── seed.py
    └── demo.py
```
