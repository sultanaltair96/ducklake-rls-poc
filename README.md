# DuckLake + PostgreSQL RLS + Service-Layer Masking - Step Zero POC

This repo demonstrates a small but realistic governance pattern:

- DuckLake stores table metadata in PostgreSQL.
- DuckLake stores the actual data as Parquet files in Azure Blob via DuckDB's Azure filesystem extension.
- PostgreSQL Row-Level Security (RLS) controls which DuckLake catalog rows a role can discover.
- A simple persona router (`AS_USER` in `scripts/demo.py`) applies row filters and SSN masking in the service/query layer.

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

CREATE POLICY allow_customer_table ON ducklake_table
    FOR SELECT
    TO ducklake_eu_analyst
    USING (table_name = 'customers');
```

In DuckLake, filtering `ducklake_table` changes which DuckLake tables are visible to a role. This POC uses that for catalog-level access control.

Important: PostgreSQL RLS here governs DuckLake **metadata discovery**, not the customer rows inside Parquet files. Row filtering and SSN masking happen in `scripts/demo.py`.

## 0.6 What you will build

```text
+--------------------------------------------------+
| scripts/demo.py                                  |
|   AS_USER maps to a safe SQL query               |
|   region filter + SSN mask happen here           |
+-------------------------+------------------------+
                          |
                          | ATTACH 'ducklake:postgres:host=... user=...'
                          v
+--------------------------------------------------+
| PostgreSQL catalog                               |
|   ducklake_table says customers exists           |
|   RLS controls catalog discovery                 |
+-------------------------+------------------------+
                          |
                          | paths in ducklake_data_file
                          v
+--------------------------------------------------+
| Azure Blob Storage                               |
|   az://ducklake-data/main/customers/*.parquet    |
+--------------------------------------------------+
```

The demo now creates one physical DuckLake table:

| Table | What it contains | Who can discover it |
|---|---|---|
| `customers` | EU + US rows with raw SSNs | all demo personas |

The persona-specific behavior is implemented in the service-layer query:

| Persona | Row filter | SSN expression |
|---|---|---|
| `admin` | none | raw `ssn` |
| `alice` | `region = 'eu'` | raw `ssn` |
| `bob` | `region = 'us'` | raw `ssn` |
| `carol` | none | `'***-**-****' AS ssn` |
| `eu-limited` | `region = 'eu'` | `'***-**-****' AS ssn` |
| `us-limited` | `region = 'us'` | `'***-**-****' AS ssn` |

This avoids the earlier non-scalable pattern of physically duplicating data into `customers_eu`, `customers_us`, and `customers_masked`.

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

Important: `init.sql` does **not** create RLS policies on `ducklake_table`. On a fresh database, `ducklake_table` does not exist until DuckLake is first attached. `scripts/seed.py` applies RLS after the catalog tables exist.

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
4. Drops old demo tables from earlier versions if they exist: `customers_eu`, `customers_us`, `customers_masked`.
5. Creates one canonical DuckLake table: `customers`.
6. Flushes tiny demo rows out of DuckLake's inlined catalog storage and into Parquet:
   ```sql
   CALL ducklake_flush_inlined_data('lake');
   ```
7. Uses `psql` to update the PostgreSQL catalog:
   ```sql
   ALTER TABLE public.ducklake_table ENABLE ROW LEVEL SECURITY;
   CREATE POLICY allow_customer_table ON public.ducklake_table
       FOR SELECT
       TO ducklake_eu_analyst, ducklake_us_analyst, ducklake_pii_reader,
          ducklake_eu_limited, ducklake_us_limited
       USING (table_name = 'customers');
   ```

After this step you should see Parquet files in Azure Blob under paths like:

```text
az://ducklake-data/main/customers/...
```

---

## 5. Step 3 - demonstrate RLS, row filtering, and masking

Open `scripts/demo.py` and change:

```python
AS_USER = "admin"
```

Valid values:

| `AS_USER` | Postgres login | Expected result |
|---|---|---|
| `admin` | `admin` | 4 raw rows from EU + US |
| `alice` | `alice` | 2 EU rows with raw SSN |
| `bob` | `bob` | 2 US rows with raw SSN |
| `carol` | `carol` | 4 rows with masked SSN |
| `eu-limited` | `eu_limited` | 2 EU rows with masked SSN |
| `us-limited` | `us_limited` | 2 US rows with masked SSN |

Run:

```bash
python scripts/demo.py
```

The script prints two things:

1. **Visible DuckLake catalog tables after Postgres RLS** - every persona should see only `customers`.
2. **The routed query result** - this shows the service-layer row filter and/or SSN mask for that persona.

This is intentionally similar to a tiny service layer: authenticated identity maps to a safe query.

---

## 6. What just happened?

- Every persona connects to the **same DuckLake**.
- The customer data is stored once in the **same Azure Blob table path**.
- PostgreSQL RLS limits catalog discovery to the single canonical table: `customers`.
- `scripts/demo.py` applies the actual row filtering and masking policy.

Example service-layer queries:

```sql
-- admin
SELECT id, name, email, ssn, region
FROM customers
ORDER BY id;

-- eu-limited
SELECT id, name, email, '***-**-****' AS ssn, region
FROM customers
WHERE region = 'eu'
ORDER BY id;
```

This is not a full Unity Catalog replacement. It is a minimal, working shape for catalog-controlled DuckLake access plus service-layer row/column policy enforcement.

---

## 7. Expected demo output shape

### `AS_USER = "admin"`

- Visible catalog tables: `customers`
- Result rows: 4
- SSNs: raw

### `AS_USER = "alice"`

- Visible catalog tables: `customers`
- Result rows: 2
- Region: `eu`
- SSNs: raw

### `AS_USER = "bob"`

- Visible catalog tables: `customers`
- Result rows: 2
- Region: `us`
- SSNs: raw

### `AS_USER = "carol"`

- Visible catalog tables: `customers`
- Result rows: 4
- SSNs: `***-**-****`

### `AS_USER = "eu-limited"`

- Visible catalog tables: `customers`
- Result rows: 2
- Region: `eu`
- SSNs: `***-**-****`

### `AS_USER = "us-limited"`

- Visible catalog tables: `customers`
- Result rows: 2
- Region: `us`
- SSNs: `***-**-****`

---

## 8. Tests

Run the fast source-level regression tests:

```bash
python -m unittest discover -v
```

These tests verify that:

- all persona queries read from the single `customers` table
- region filtering and masking are encoded as expected
- `seed.py` creates only one customer DuckLake table

---

## 9. Troubleshooting

- **`psql not found`** - install PostgreSQL client tools. `seed.py` uses `psql` to apply catalog RLS after DuckLake creates the catalog tables.
- **`ducklake_table does not exist` during init** - you are using an old README/script. Current `init.sql` does not touch `ducklake_table`; `seed.py` does.
- **`Missing required environment variable: AZURE_STORAGE_KEY`** - fill `.env` from `.env.example`.
- **`IO Error ... blob.core.windows.net`** - check `AZURE_STORAGE_ACCOUNT`, `AZURE_STORAGE_KEY`, and `DATA_PATH`.
- **Personas see no catalog tables** - rerun `python scripts/seed.py`; the RLS policy is applied there.

---

## File layout

```text
ducklake-rls-poc/
├── .env.example
├── README.md
├── sql/
│   └── init.sql
├── scripts/
│   ├── seed.py
│   └── demo.py
└── tests/
    └── test_single_table_policy_model.py
```
