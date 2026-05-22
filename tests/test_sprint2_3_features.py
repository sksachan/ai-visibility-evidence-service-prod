"""Tests for Sprint 2-3 features: portfolio upload, brand config CRUD, and schema validation."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Set DATA_DIR before importing app modules
TEST_DATA_DIR = tempfile.mkdtemp(prefix="test_sprint23_")
os.environ["DATA_DIR"] = TEST_DATA_DIR
os.environ["ADMIN_TOKEN"] = ""

from app.portfolio_schema import (
    validate_portfolio,
    normalise_uploaded_portfolio,
    generate_portfolio_template,
    SCHEMA_VERSION,
)
from app.brand_config import (
    save_brand_config,
    load_brand_config,
    list_brand_configs,
    delete_brand_config,
    BrandConfigRequest,
)


class TestPortfolioValidation(unittest.TestCase):
    """Test portfolio schema validation."""

    def test_valid_portfolio(self):
        portfolio = {
            "topics": [{"topic": "Electric vehicles"}],
            "queries": [
                {"query_id": "q001", "query": "best electric SUV 2025", "query_type": "non_branded", "journey_stage": "consideration"},
                {"query_id": "q002", "query": "Toyota EV range", "query_type": "branded", "journey_stage": "research"},
            ],
            "brand": "Toyota",
            "market": "Germany",
        }
        result = validate_portfolio(portfolio)
        self.assertTrue(result["valid"])
        self.assertEqual(result["stats"]["topic_count"], 1)
        self.assertEqual(result["stats"]["query_count"], 2)
        self.assertEqual(len(result["errors"]), 0)

    def test_empty_topics_fails(self):
        portfolio = {"topics": [], "queries": [{"query": "test"}]}
        result = validate_portfolio(portfolio)
        self.assertFalse(result["valid"])
        self.assertTrue(any("topics" in e for e in result["errors"]))

    def test_empty_queries_fails(self):
        portfolio = {"topics": ["EV"], "queries": []}
        result = validate_portfolio(portfolio)
        self.assertFalse(result["valid"])
        self.assertTrue(any("queries" in e for e in result["errors"]))

    def test_invalid_journey_stage_error(self):
        portfolio = {
            "topics": ["EV"],
            "queries": [{"query": "test", "journey_stage": "invalid_stage"}],
        }
        result = validate_portfolio(portfolio)
        self.assertFalse(result["valid"])
        self.assertTrue(any("journey_stage" in e for e in result["errors"]))

    def test_invalid_query_type_error(self):
        portfolio = {
            "topics": ["EV"],
            "queries": [{"query": "test", "query_type": "invalid_type"}],
        }
        result = validate_portfolio(portfolio)
        self.assertFalse(result["valid"])
        self.assertTrue(any("query_type" in e for e in result["errors"]))

    def test_duplicate_query_id_warning(self):
        portfolio = {
            "topics": ["EV"],
            "queries": [
                {"query_id": "q001", "query": "test 1"},
                {"query_id": "q001", "query": "test 2"},
            ],
        }
        result = validate_portfolio(portfolio)
        self.assertTrue(result["valid"])  # Warnings don't fail validation
        self.assertTrue(any("duplicate query_id" in w for w in result["warnings"]))

    def test_string_topics_and_queries(self):
        portfolio = {
            "topics": ["Electric vehicles", "Safety"],
            "queries": ["best EV 2025", "safest car"],
        }
        result = validate_portfolio(portfolio)
        self.assertTrue(result["valid"])

    def test_missing_query_text_error(self):
        portfolio = {
            "topics": ["EV"],
            "queries": [{"query_id": "q001"}],  # Missing 'query' field
        }
        result = validate_portfolio(portfolio)
        self.assertFalse(result["valid"])

    def test_non_dict_payload_fails(self):
        result = validate_portfolio("not a dict")
        self.assertFalse(result["valid"])


class TestPortfolioNormalisation(unittest.TestCase):
    """Test portfolio normalisation."""

    def test_normalise_adds_schema_version(self):
        portfolio = {"topics": ["EV"], "queries": ["test"]}
        result = normalise_uploaded_portfolio(portfolio, brand="Toyota", market="Germany")
        self.assertEqual(result["schema_version"], SCHEMA_VERSION)
        self.assertEqual(result["brand"], "Toyota")
        self.assertEqual(result["market"], "Germany")

    def test_normalise_assigns_query_ids(self):
        portfolio = {"topics": ["EV"], "queries": [{"query": "test 1"}, {"query": "test 2"}]}
        result = normalise_uploaded_portfolio(portfolio)
        self.assertEqual(result["queries"][0]["query_id"], "q001")
        self.assertEqual(result["queries"][1]["query_id"], "q002")

    def test_normalise_preserves_existing_ids(self):
        portfolio = {"topics": ["EV"], "queries": [{"query_id": "custom_1", "query": "test"}]}
        result = normalise_uploaded_portfolio(portfolio)
        self.assertEqual(result["queries"][0]["query_id"], "custom_1")

    def test_normalise_string_topics(self):
        portfolio = {"topics": ["EV", "Safety"], "queries": ["test"]}
        result = normalise_uploaded_portfolio(portfolio)
        self.assertEqual(result["topics"][0], {"topic": "EV"})
        self.assertEqual(result["topics"][1], {"topic": "Safety"})

    def test_normalise_sets_portfolio_source(self):
        portfolio = {"topics": ["EV"], "queries": ["test"]}
        result = normalise_uploaded_portfolio(portfolio)
        self.assertEqual(result["portfolio_source"], "user_upload")


class TestPortfolioTemplate(unittest.TestCase):
    """Test portfolio template generation."""

    def test_template_has_required_fields(self):
        template = generate_portfolio_template(brand="BMW", market="UK")
        self.assertEqual(template["brand"], "BMW")
        self.assertEqual(template["market"], "UK")
        self.assertIsInstance(template["topics"], list)
        self.assertIsInstance(template["queries"], list)
        self.assertTrue(len(template["topics"]) > 0)
        self.assertTrue(len(template["queries"]) > 0)

    def test_template_defaults(self):
        template = generate_portfolio_template()
        self.assertEqual(template["brand"], "YourBrand")
        self.assertEqual(template["market"], "YourMarket")

    def test_template_is_valid(self):
        template = generate_portfolio_template(brand="Test", market="US")
        result = validate_portfolio(template)
        self.assertTrue(result["valid"], f"Template should be valid but got errors: {result['errors']}")


class TestBrandConfigCRUD(unittest.TestCase):
    """Test brand configuration CRUD operations."""

    def setUp(self):
        """Clean up brand config dir before each test."""
        config_dir = Path(TEST_DATA_DIR) / "brand_configs"
        if config_dir.exists():
            shutil.rmtree(config_dir)

    def test_create_brand_config(self):
        req = BrandConfigRequest(
            brand="Toyota",
            market="Germany",
            domain="https://www.toyota.de",
            owned_domains=["toyota.de", "www.toyota.de"],
            brand_terms=["Toyota", "Corolla", "RAV4"],
            language="German",
        )
        config = save_brand_config(req)
        self.assertEqual(config["brand"], "Toyota")
        self.assertEqual(config["market"], "Germany")
        self.assertEqual(config["domain"], "https://www.toyota.de")
        self.assertIn("toyota.de", config["owned_domains"])
        self.assertIn("config_id", config)

    def test_load_brand_config(self):
        req = BrandConfigRequest(brand="BMW", market="UK", domain="https://www.bmw.co.uk")
        save_brand_config(req)
        loaded = load_brand_config("BMW", "UK")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["brand"], "BMW")
        self.assertEqual(loaded["market"], "UK")

    def test_load_nonexistent_config(self):
        loaded = load_brand_config("NonExistent", "Nowhere")
        self.assertIsNone(loaded)

    def test_list_brand_configs(self):
        save_brand_config(BrandConfigRequest(brand="Toyota", market="Japan"))
        save_brand_config(BrandConfigRequest(brand="BMW", market="Germany"))
        save_brand_config(BrandConfigRequest(brand="Ford", market="USA"))
        configs = list_brand_configs()
        self.assertEqual(len(configs), 3)
        brands = [c["brand"] for c in configs]
        self.assertIn("Toyota", brands)
        self.assertIn("BMW", brands)
        self.assertIn("Ford", brands)

    def test_update_brand_config(self):
        req1 = BrandConfigRequest(brand="Toyota", market="Japan", language="Japanese")
        config1 = save_brand_config(req1)
        config_id = config1["config_id"]

        req2 = BrandConfigRequest(brand="Toyota", market="Japan", language="English")
        config2 = save_brand_config(req2)
        self.assertEqual(config2["config_id"], config_id)  # Same config_id
        self.assertEqual(config2["language"], "English")

    def test_delete_brand_config(self):
        save_brand_config(BrandConfigRequest(brand="Toyota", market="Japan"))
        self.assertTrue(delete_brand_config("Toyota", "Japan"))
        self.assertIsNone(load_brand_config("Toyota", "Japan"))

    def test_delete_nonexistent_config(self):
        self.assertFalse(delete_brand_config("NonExistent", "Nowhere"))

    def test_config_defaults(self):
        req = BrandConfigRequest(brand="Test", market="US")
        config = save_brand_config(req)
        self.assertEqual(config["language"], "English")
        self.assertEqual(config["default_topic_count"], 8)
        self.assertEqual(config["default_queries_per_topic"], 6)
        self.assertEqual(config["default_query_limit"], 50)


class TestBrandConfigSorted(unittest.TestCase):
    """Test that brand configs are sorted alphabetically."""

    def setUp(self):
        config_dir = Path(TEST_DATA_DIR) / "brand_configs"
        if config_dir.exists():
            shutil.rmtree(config_dir)

    def test_list_sorted_by_brand_market(self):
        save_brand_config(BrandConfigRequest(brand="Zebra", market="Africa"))
        save_brand_config(BrandConfigRequest(brand="Apple", market="USA"))
        save_brand_config(BrandConfigRequest(brand="BMW", market="Germany"))
        configs = list_brand_configs()
        brands = [c["brand"] for c in configs]
        self.assertEqual(brands, ["Apple", "BMW", "Zebra"])


if __name__ == "__main__":
    unittest.main()
