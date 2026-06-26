# DuckLake + PostgreSQL RLS + Column Masking - A Zero-to-Hero Tutorial

This is a hands-on POC that walks you through, in order:

1. **What DuckLake is** and why the catalog matters
2. **What Row-Level Security (RLS)** is and why it is the natural place to enforce access in DuckLake
3. **Prerequisites** - Postgres + Azure Blob
4. **Step 1:** Install and configure Postgres, create the demo roles
5. **Step 2:** Attach a DuckLake from Python, pointing at Azure Blob
6. **Step 3:** Insert data, watch Parquet files materialise in Azure
7. **Step 4:** Enable RLS on the catalog, define policies
8. **Step 5:** Add a persona view for column masking
9. **Step 6:** Prove it works - same SQL, four different results

By the end you will have run a single query four times and seen four different result sets, with no per-role code paths.

---

## 0. What is DuckLake?

DuckLake is an **open table format** from the DuckDB team. It is defined by two pieces:

- **A catalog** - a normal SQL database (here: PostgreSQL) that stores metadata as regular SQL tables. There are 28 of them in DuckLake v1.0. Examples: `ducklake_snapshot`, `ducklake_data_file`, `ducklake_table`, `ducklake_column`.
- **A data layer** - Parquet files. They live anywhere: local disk, S3, Azure Blob, GCS, NFS. The catalog stores the file paths.

The crucial rule: **files are never modified**. Every change is a new Parquet file + a new row in the catalog. Snapshots (monotonically increasing integers) version every change. This makes time travel, concurrent writes, and cheap storage simple.

The **read path** is the choke point for everything. When DuckDB executes `SELECT * FROM customers`, it:

1. Looks at `ducklake_table` to find the table.
2. Looks at `ducklake_data_file` to find which Parquet files contain the table.
3. Reads those Parquet files, joins with any delete files, returns the rows.

Because step 2 goes through the **catalog** (a Postgres table), **anything that filters which catalog rows a user can see also filters which Parquet files they can read**. That is the whole trick. No need for a custom engine; the existing Postgres permission system does the work.

## 0.5 What is Row-Level Security (RLS)?

PostgreSQL RLS lets you write policies on a table that decide which rows a given role can `SELECT` / `INSERT` / `UPDATE` / `DELETE`. Example:

```sql
ALTER TABLE ducklake_table ENABLE ROW LEVEL SECURITY;

CREATE POLICY region_eu ON ducklake_table
    AS RESTRICTIVE
    FOR ALL
    TO ducklake_eu_analyst
    USING (region = 'eu');
```

After this, when a user in the `ducklake_eu_analyst` role runs **any query** that touches `ducklake_table`, Postgres silently rewrites it to add `AND region = 'eu'`. The user does not see the rewrite. They do not see the other rows. The application does not need to know about it.

For DuckLake, this is gold: filter `ducklake_table` rows by region -> user only sees files tagged with that region -> user only reads data from those files. The Parquet files themselves never change.

**Column masking** is the same idea but for columns. The simplest demonstration: a SQL view that the role is allowed to query, that returns the same data but with sensitive columns replaced by literal placeholders. The base table is hidden from the role; the view is what they get.

## 0.6 What you will build

```
+--------------------------------------------------+
|  scripts/demo.py  (you change AS_USER)           |
|       |                                          |
|       |  ATTACH 'ducklake:postgres://<role>@pg'
|       v                                          |
|  +--------------------------+                    |
|  |  PostgreSQL              |                    |
|  |  ducklake_table (RLS on) |                    |
|  |  customers_masked (view) |                    |
|  +-----------+----------------------------------+
|              | points to (s3:// paths)
|              v
|  +--------------------------+
|  |  Azure Blob Storage      |
|  |  container: ducklake-data|
|  |    main/                 |
|  |      customers_eu/       |
|  |        ducklake-*.parquet|
|  |      customers_us/       |
|  |        ducklake-*.parquet|
|  +--------------------------+
+--------------------------------------------------+
```

The Parquet files are byte-identical across every run. What changes is which Postgres role authenticates the ATTACH, which controls which catalog rows are visible, which controls which Parquet files DuckDB reads.

---

## 1. Prerequisites

You need:

1. **PostgreSQL 12+** running locally (or anywhere reachable from your machine). On macOS: `brew install postgresql@16 && brew services start postgresql@16`. On Debian/Ubuntu: `sudo apt install postgresql-16`. On Windows: install via the official installer.
2. **Python 3.10+** with `pip install duckdb python-dotenv`.
3. **An Azure storage account** with a blob container created.

For Azure:
- In the Azure portal, create a **Storage account** (any name).
- Go to **Security + networking -> Access keys**. Click **Show** next to `key1` and copy the **Storage account name** and the **key** value.
- Go to **Data storage -> Containers** and create a container called `ducklake-data` (or any name you like).

## 2. Configure the POC

```bash
cd ducklake-rls-poc
cp .env.example .env
```

Open `.env` in an editor. Fill in the values:

```bash
# Postgres (where your local Postgres listens)
PG_HOST=localhost
PG_PORT=5432
PG_DB=ducklake_catalog
PG_ADMIN_USER=postgres
PG_ADMIN_PW=                  # leave blank for peer/trust auth; set if you have a password

# Azure Blob Storage (S3-compatible endpoint)
AZURE_STORAGE_ACCOUNT=youraccountname
AZURE_STORAGE_KEY=yourkeyvalue
AZURE_CONTAINER=ducklake-data
S3_ENDPOINT=https://youraccountname.blob.core.windows.net
S3_REGION=eastus
DATA_PATH=s3://ducklake-data/
```

Save and close.

## 3. Step 1 - initialize Postgres

The first script (`sql/init.sql`) creates:

- The `ducklake_catalog` database
- Four **group roles** (no login): `ducklake_admin`, `ducklake_eu_analyst`, `ducklake_us_analyst`, `ducklake_pii_reader`
- Four **login users**: `admin`, `alice`, `bob`, `carol` (each in one of the group roles above)
- Two **RLS policies** on the future `ducklake_table`: one for EU, one for US
- Default privileges so the ducklake extension's auto-created catalog tables are accessible

Run it:

```bash
psql -U postgres -f sql/init.sql
```

If your local Postgres uses peer auth, you may need `sudo -u postgres psql -f sql/init.sql` instead.

You should see output like `CREATE ROLE`, `CREATE USER`, `CREATE POLICY`. If you see errors about the database already existing or roles already existing, that is fine - the script is idempotent.

Sanity check: connect as alice and confirm she has no direct access yet:

```bash
psql -U alice -d ducklake_catalog -c "SELECT 1;"
# Should work (basic login)
psql -U alice -d ducklake_catalog -c "SELECT * FROM ducklake_table;"
# Should fail with "permission denied" - the table does not exist yet, and even if it did, RLS would filter
```

## 4. Step 2 - run the seed script

This is where DuckLake actually gets created. Run:

```bash
python scripts/seed.py
```

What it does, in order:

1. Opens an in-memory DuckDB and installs the `ducklake` and `httpfs` extensions.
2. Creates an **Azure S3 secret** that points DuckDB at your Azure storage account. This is the same pattern DuckDB uses for S3 / GCS / R2 - the `ENDPOINT` parameter is what routes it to Azure instead of AWS.
3. **Attaches the DuckLake** as the Postgres admin user:
   ```sql
   ATTACH 'ducklake:postgres://postgres@localhost/ducklake_catalog'
       AS lake (DATA_PATH 's3://ducklake-data/');
   USE lake;
   ```
   This is the magic moment. From this point on, every catalog table (`ducklake_table`, `ducklake_data_file`, etc.) is just a regular SQL table in Postgres that DuckDB can query.
4. **Adds a `region` column** to `ducklake_table` if missing. The RLS policies from step 3 reference this column.
5. **Enables RLS** on `ducklake_table`:
   ```sql
   ALTER TABLE ducklake_table ENABLE ROW LEVEL SECURITY;
   ```
6. **Creates two tables and inserts data**:
   ```sql
   CREATE TABLE customers (id INT, name TEXT, email TEXT, ssn TEXT, region TEXT);
   INSERT INTO customers VALUES
       (1, 'Maria Schmidt', '[email protected]', 'DE-123456789', 'eu'),
       (2, 'Lars Eriksen',  '[email protected]', 'NO-987654321', 'eu'),
       (3, 'John Smith',    '[email protected]', 'US-555-44-3333', 'us'),
       (4, 'Emily Johnson', '[email protected]', 'US-111-22-3333', 'us');
   ```
7. **Splits into region-specific tables** and drops the original:
   ```sql
   CREATE TABLE customers_eu AS SELECT * FROM customers WHERE region = 'eu';
   CREATE TABLE customers_us AS SELECT * FROM customers WHERE region = 'us';
   DROP TABLE customers;
   ```
8. **Tags each table in the catalog**:
   ```sql
   UPDATE ducklake_table SET region = 'eu' WHERE table_name = 'customers_eu';
   UPDATE ducklake_table SET region = 'us' WHERE table_name = 'customers_us';
   ```
   These `region` values on `ducklake_table` are what the RLS policies match against.
9. **Creates the masked view for carol**:
   ```sql
   CREATE VIEW customers_masked AS
   SELECT id, name, email, '***-**-****' AS ssn, region FROM customers_eu
   UNION ALL
   SELECT id, name, email, '***-**-****' AS ssn, region FROM customers_us;
   ```

You will see Parquet files materialising in your Azure container. Confirm in the Azure portal: **Storage account -> Containers -> ducklake-data -> main/customers_eu/** should show one or more `ducklake-*.parquet` files.

---

## 5. Run the demo - same query, four roles

Open `scripts/demo.py` in an editor. Near the top:

```python
# CHANGE THIS to switch roles.
# Valid: "admin" (sees all), "alice" (EU only), "bob" (US only),
#        "carol" (all rows, PII masked)
AS_USER = "admin"
```

`demo.py` does the same things as `seed.py` (install extensions, configure Azure, ATTACH the DuckLake) **but as the role you specify in `AS_USER`**. Because the ATTACH line uses that role's Postgres credentials, the connection inherits the role's permissions - including the RLS policies.

It then runs this query:

```sql
SELECT id, name, email, ssn, region FROM customers_eu
UNION ALL
SELECT id, name, email, ssn, region FROM customers_us
ORDER BY id
```

The same string every time. No per-role code path.

### Run 1 - admin (sees everything)

```python
AS_USER = "admin"
python scripts/demo.py
```

Expected output: 4 rows (2 EU + 2 US), real SSN values.

### Run 2 - alice (EU only)

```python
AS_USER = "alice"
python scripts/demo.py
```

Expected output: **2 rows** (Maria, Lars), no US rows. The catalog returned only `customers_eu` because the RLS policy filtered `ducklake_table` to `region = 'eu'`. The US Parquet file is never read.

### Run 3 - bob (US only)

```python
AS_USER = "bob"
python scripts/demo.py
```

Expected output: **2 rows** (John, Emily).

### Run 4 - carol (PII masked)

```python
AS_USER = "carol"
python scripts/demo.py
```

Expected output: **4 rows** (all of them) but `ssn` shows `***-**-****` for every row. The persona view is doing the work, not RLS - carol has no row-level policy.

---

## 6. What just happened (the punchline)

1. **The Parquet files in Azure never moved.** They are byte-identical between admin's run and carol's run.
2. **The SQL string never changed.** Same query, four times.
3. **The DuckDB extension never changed.** Same `ducklake` extension, same `ATTACH` syntax.
4. **What changed is which Postgres role authenticated the connection.** That role decides which rows the catalog returns, and the catalog decides which Parquet files DuckDB reads.

That is the whole POC. One line in DuckDB - the `ATTACH` with the role's credentials - combined with one Postgres mechanism - RLS - combined with one view for column masking. Total: ~80 lines of SQL + ~150 lines of Python.

---

## 7. Going further

Each of these is a follow-up POC, not a hidden gotcha:

- **Production auth.** Replace the `AS_USER` variable with a real identity layer (JWT, session, mTLS) that maps to one of the four logins.
- **Audit log.** A few `AFTER` triggers on `ducklake_snapshot` that record who accessed what.
- **Multi-engine governance.** This POC enforces policy only on the DuckDB query path. Polars, PyArrow, or Spark pointing at the same Parquet files would bypass RLS entirely. To enforce across engines, you would need either DuckLake-aware connectors or a storage-layer enforcement (scoped SAS tokens per user).
- **Encryption.** Add `ATTACH '...' (ENCRYPTED)`. Per-file keys are stored in the catalog, so a leaked Azure credential still can't decrypt the data without the catalog.
- **Migration from DuckDB.** See the `ducklake-expertise` skill's section on migration. The TL;DR: `COPY FROM DATABASE my_db TO my_lake`.

## Troubleshooting

- **`role "ducklake_admin" does not exist`** - re-run `sql/init.sql`; it is idempotent.
- **`permission denied for table ducklake_table`** - the demo/seed user does not have the right role. Check `\du` in psql.
- **`IO Error: Could not connect to ... blob.core.windows.net`** - wrong `S3_ENDPOINT` or `AZURE_STORAGE_KEY`. Test with `az storage blob list --account-name <name> --account-key <key>`.
- **Demo shows all 4 rows for everyone** - RLS is not on. Check `SELECT relrowsecurity FROM pg_class WHERE relname = 'ducklake_table';` - should be `t`.
- **`extension "ducklake" is not installed`** - the ducklake extension ships with `pip install duckdb` v1.5.2+. Older versions need an explicit `INSTALL ducklake;` which `seed.py` already runs.

## File layout

```
ducklake-rls-poc/
├── .env.example       # template for Postgres + Azure config
├── README.md          # this file
├── sql/
│   └── init.sql       # creates roles, login users, RLS policies
└── scripts/
    ├── seed.py        # one-time bootstrap: schema, RLS, data, Azure files
    └── demo.py        # the demo: change AS_USER, run, observe
```

## Why "PostgreSQL + RLS" and not "Unity Catalog"?

Honest answer: it is not as good as Unity Catalog. Unity Catalog enforces policy across every engine (Spark, Polars, notebooks, BI). This POC enforces policy only on the DuckDB query path. For a one-person or small-team project, that is sufficient. The path toward Unity-Catalog-equivalent governance is documented in the `ducklake-expertise` skill.
