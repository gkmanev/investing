import json

from django.test import SimpleTestCase

from api.response_blocks import BlockValidationError, table_block_from_tool_result, validate_table_block


class ResponseBlockTests(SimpleTestCase):
    def test_builds_validated_table_from_ranked_tool_results(self) -> None:
        tool_result = json.dumps({
            "opportunities": [
                {
                    "ticker": "AAPL",
                    "underlying_price": 210.5,
                    "rsi": 47.3,
                    "strike": 205.0,
                    "expiration": "2026-08-21",
                    "dte": 30,
                    "roi": 2.3,
                    "stock_quality_score": 87,
                }
            ]
        })

        block = table_block_from_tool_result("scan_put_opportunities", tool_result)

        self.assertEqual(block["type"], "table")
        self.assertEqual(block["version"], 1)
        self.assertEqual(block["title"], "Ranked opportunities")
        self.assertEqual(block["columns"][2]["label"], "Price")
        self.assertEqual([column["key"] for column in block["columns"]], [
            "rank", "ticker", "current_price", "strike", "expiration_dte", "roi", "rsi", "stock_quality_score",
        ])
        self.assertEqual(block["rows"], [{
            "rank": 1,
            "ticker": "AAPL",
            "current_price": 210.5,
            "rsi": 47.3,
            "strike": 205.0,
            "expiration_dte": "2026-08-21 (30 DTE)",
            "roi": 2.3,
            "stock_quality_score": 87,
        }])

    def test_rejects_unknown_row_fields_and_wrong_cell_types(self) -> None:
        candidate = {
            "type": "table",
            "columns": [{"key": "price", "label": "Price", "type": "currency"}],
            "rows": [{"price": "not-a-number", "unsafe": "value"}],
        }

        with self.assertRaises(BlockValidationError):
            validate_table_block(candidate)

    def test_builds_table_from_monthly_income_plan_allocations(self) -> None:
        tool_result = json.dumps({
            "plan_type": "cash_secured_puts_only",
            "allocated_put_ideas": [{
                "ticker": "OKTA",
                "underlying_price": 141.71,
                "strike": 130.0,
                "expiration": "2026-08-21",
                "dte": 30,
                "delta": -0.2865,
                "premium_received": 545.0,
                "cash_required": 13000.0,
                "contracts_affordable": 1,
                "estimated_monthly_income": 545.0,
                "stock_quality_score": 84.0,
            }],
        })

        block = table_block_from_tool_result("build_monthly_income_plan", tool_result)

        self.assertEqual(block["title"], "Monthly income plan")
        self.assertEqual([column["key"] for column in block["columns"]], [
            "rank", "ticker", "current_price", "strike", "expiration_dte", "delta",
            "premium_received", "cash_required", "contracts_affordable",
            "estimated_monthly_income", "stock_quality_score",
        ])
        self.assertEqual(block["rows"], [{
            "rank": 1,
            "ticker": "OKTA",
            "current_price": 141.71,
            "strike": 130.0,
            "expiration_dte": "2026-08-21 (30 DTE)",
            "delta": -0.2865,
            "premium_received": 545.0,
            "cash_required": 13000.0,
            "contracts_affordable": 1,
            "estimated_monthly_income": 545.0,
            "stock_quality_score": 84.0,
        }])

    def test_keeps_expiration_column_when_dte_is_not_provided(self) -> None:
        tool_result = json.dumps({
            "opportunities": [{
                "ticker": "AAPL",
                "expiration": "2026-08-21",
            }],
        })

        block = table_block_from_tool_result("scan_put_opportunities", tool_result)

        self.assertEqual([column["key"] for column in block["columns"]], [
            "rank", "ticker", "expiration",
        ])
        self.assertEqual(block["rows"][0]["expiration"], "2026-08-21")

    def test_ignores_unstructured_or_empty_tool_results(self) -> None:
        self.assertIsNone(table_block_from_tool_result("scan_put_opportunities", "not json"))
        self.assertIsNone(table_block_from_tool_result("scan_put_opportunities", json.dumps({"opportunities": []})))
