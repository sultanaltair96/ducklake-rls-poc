"""Regression tests for the scalable one-table policy model.

These tests intentionally avoid Azure/Postgres integration. They verify the POC's
policy shape from source-level contracts so the fast feedback loop can run
without touching cloud storage.
"""

import os
from pathlib import Path
import unittest

# Make the source-level tests self-contained. Importing scripts.demo reads these
# environment variables at module import time, but these unit tests do not touch
# Azure or PostgreSQL.
os.environ.setdefault("DATA_PATH", "az://unit-test/")
os.environ.setdefault("AZURE_STORAGE_ACCOUNT", "unit-test")
os.environ.setdefault("AZURE_STORAGE_KEY", "unit-test")

import scripts.demo as demo


class SingleTablePolicyModelTests(unittest.TestCase):
    def test_all_persona_queries_read_from_one_customers_table(self):
        for persona, (_pg_user, _pw, sql) in demo.USERS.items():
            with self.subTest(persona=persona):
                self.assertIn("FROM customers", sql)
                self.assertNotIn("customers_eu", sql)
                self.assertNotIn("customers_us", sql)
                self.assertNotIn("customers_masked", sql)

    def test_persona_queries_encode_expected_region_and_masking_policy(self):
        expectations = {
            "admin": {"region": None, "masked": False},
            "alice": {"region": "eu", "masked": False},
            "bob": {"region": "us", "masked": False},
            "carol": {"region": None, "masked": True},
            "eu-limited": {"region": "eu", "masked": True},
            "us-limited": {"region": "us", "masked": True},
        }

        for persona, expected in expectations.items():
            with self.subTest(persona=persona):
                sql = demo.USERS[persona][2]
                if expected["region"] is None:
                    self.assertNotIn("WHERE region", sql)
                else:
                    self.assertIn("WHERE region = '" + expected["region"] + "'", sql)

                if expected["masked"]:
                    self.assertIn("'***-**-****' AS ssn", sql)
                    self.assertNotIn(" email, ssn, region", sql)
                else:
                    self.assertIn(" email, ssn, region", sql)
                    self.assertNotIn("'***-**-****' AS ssn", sql)

    def test_seed_creates_only_one_customer_ducklake_table(self):
        seed_source = Path("scripts/seed.py").read_text()
        self.assertIn("CREATE OR REPLACE TABLE customers AS", seed_source)
        self.assertNotIn("CREATE OR REPLACE TABLE customers_eu", seed_source)
        self.assertNotIn("CREATE OR REPLACE TABLE customers_us", seed_source)
        self.assertNotIn("CREATE OR REPLACE TABLE customers_masked", seed_source)


if __name__ == "__main__":
    unittest.main()
