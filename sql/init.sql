-- DuckLake RLS POC - Postgres initial setup
--
-- Run once as a Postgres superuser:
--   psql -U postgres -f sql/init.sql
--
-- This script deliberately does NOT create RLS policies on ducklake_table.
-- DuckLake creates ducklake_table during the first ATTACH, so seed.py applies
-- the policies after the catalog tables exist.

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

-- Catalog tables are auto-created by DuckLake on first ATTACH. These grants
-- apply to future objects created by the role that runs this init script.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO
    ducklake_admin, ducklake_eu_analyst, ducklake_us_analyst, ducklake_pii_reader;

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO
    ducklake_admin, ducklake_eu_analyst, ducklake_us_analyst, ducklake_pii_reader;

-- =====================================================================
-- 2. Login roles (the demo script authenticates as these)
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
