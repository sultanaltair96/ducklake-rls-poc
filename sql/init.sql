-- DuckLake RLS POC - Postgres initial setup
--
-- Run this once as a Postgres superuser (e.g. `psql -U postgres -f sql/init.sql`).
-- It creates the database, the four demo login users, the role groups,
-- and the RLS policies on the catalog tables.
--
-- After this script runs, the ducklake extension will create the
-- ducklake_* catalog tables automatically the first time someone runs
-- `ATTACH 'ducklake:postgres://...'` -- you don't need to create them here.

-- =====================================================================
-- 0. Database (idempotent)
-- =====================================================================

SELECT 'CREATE DATABASE ducklake_catalog'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'ducklake_catalog')\gexec

\connect ducklake_catalog

-- =====================================================================
-- 1. Group roles (used for RLS policies)
-- =====================================================================

DO $$ BEGIN
    CREATE ROLE ducklake_admin NOLOGIN BYPASSRLS;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE ROLE ducklake_eu_analyst NOLOGIN;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE ROLE ducklake_us_analyst NOLOGIN;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE ROLE ducklake_pii_reader NOLOGIN;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

GRANT USAGE ON SCHEMA public TO
    ducklake_admin, ducklake_eu_analyst, ducklake_us_analyst, ducklake_pii_reader;

-- Catalog tables are auto-created by the ducklake extension on first ATTACH,
-- so we use ALTER DEFAULT PRIVILEGES to grant future tables to our roles.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO
    ducklake_admin, ducklake_eu_analyst, ducklake_us_analyst, ducklake_pii_reader;

-- Also grant on any tables that already exist (e.g. ducklake_* if the
-- extension was loaded before this script was run).
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO
    ducklake_admin, ducklake_eu_analyst, ducklake_us_analyst, ducklake_pii_reader;

-- =====================================================================
-- 2. Login roles (the service / demo script authenticates as these)
-- =====================================================================
-- Passwords are POC-only. In production: pull from a secret store.

DO $$ BEGIN
    CREATE USER alice WITH PASSWORD 'alice_pw' IN ROLE ducklake_eu_analyst;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE USER bob WITH PASSWORD 'bob_pw' IN ROLE ducklake_us_analyst;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE USER carol WITH PASSWORD 'carol_pw' IN ROLE ducklake_pii_reader;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE USER admin WITH PASSWORD 'admin_pw' IN ROLE ducklake_admin SUPERUSER;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- =====================================================================
-- 3. RLS policies
-- =====================================================================
-- Defined as RESTRICTIVE so admin (BYPASSRLS) skips them, and they
-- compose with any future permissive policies.
-- The `region` column is added to ducklake_table by seed.py.

DO $$ BEGIN
    CREATE POLICY region_eu ON ducklake_table
        AS RESTRICTIVE
        FOR ALL
        TO ducklake_eu_analyst
        USING (region = 'eu')
        WITH CHECK (region = 'eu');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE POLICY region_us ON ducklake_table
        AS RESTRICTIVE
        FOR ALL
        TO ducklake_us_analyst
        USING (region = 'us')
        WITH CHECK (region = 'us');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- pii_reader has no row restrictions; column masking is done in seed.py
-- by creating a `customers_masked` view that pii_reader queries instead
-- of the base table.
