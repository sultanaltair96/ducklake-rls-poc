# DuckLake + PostgreSQL RLS POC

A proof-of-concept demonstrating:

- **DuckLake v1.0** as the open table format (Parquet data + catalog in PostgreSQL)
- **PostgreSQL Row-Level Security** policies on the DuckLake catalog tables
- A **tiny query service** that connects to DuckLake as the requesting user, so RLS is enforced

## What this proves

A single SQL query (`SELECT * FROM customers`) returns **different rows** when run as different roles, because the catalog's RLS policy filters which data files are visible. The Parquet files never change; the catalog is the choke point.

## Architecture

```
┌─────────────────────────────────────────────────────┐
│  Client (curl / app)                                │
│     │                                               │
│     │  GET /query?sql=...&as=alice                  │
│     ▼                                               │
│  ┌──────────────────────────────────────────┐       │
│  │  Service (Python + FastAPI)              │       │
│  │  - Auth: extracts "as=alice" from req    │       │
│  │  - Connects to Postgres as alice         │       │
│  │  - Attaches DuckLake catalog             │       │
│  │  - Runs user's SQL via DuckDB            │       │
│  └──────────────┬───────────────────────────┘       │
│                 │                                   │
│      ┌──────────┴──────────┐                        │
│      ▼                     ▼                        │
│  ┌─────────────┐    ┌──────────────────┐            │
│  │ PostgreSQL  │    │ Local Parquet    │            │
│  │ (catalog +  │    │ data directory   │            │
│  │  RLS)       │    │                  │            │
│  └─────────────┘    └──────────────────┘            │
└─────────────────────────────────────────────────────┘
```

## Roles in this POC

| Role | Sees |
|---|---|
| `admin` | All data, all schemas, all snapshots |
| `analyst_eu` | Only rows tagged `region = 'eu'` |
| `analyst_us` | Only rows tagged `region = 'us'` |
| `reader_masked` | All rows, but `ssn` column comes back as `***-**-****` |

## Quickstart

```bash
# 1. Start Postgres
docker compose up -d postgres

# 2. Initialize the catalog, create roles, attach DuckLake, insert data
./scripts/seed.sh

# 3. Start the query service
docker compose up --build service

# 4. Try the same query as different users
curl 'http://localhost:8000/query?as=admin&sql=SELECT+count(*)+FROM+customers'
curl 'http://localhost:8000/query?as=analyst_eu&sql=SELECT+count(*)+FROM+customers'
curl 'http://localhost:8000/query?as=analyst_us&sql=SELECT+count(*)+FROM+customers'
curl 'http://localhost:8000/query?as=reader_masked&sql=SELECT+*+FROM+customers+LIMIT+1'
```

The point: counts and rows differ per role, but the Parquet files and the SQL never change.

## What's NOT in this POC

- S3 / R2 / GCS storage (data lives in a local volume; trivial to swap)
- Audit log (a few SQL triggers away)
- Multi-engine governance (only DuckDB is enforced)
- Discovery UI
- Production auth (the service trusts the `?as=` parameter — replace with real auth in production)

This is the minimum surface that proves the pattern. Each "not in" item is a follow-up POC.

## Status

POC scaffold. See `scripts/seed.sh` and `service/main.py` for the actual implementation.
