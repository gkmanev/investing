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
        self.assertEqual([column["key"] for column in block["columns"]], [
            "rank", "ticker", "current_price", "strike", "expiration", "dte", "roi", "stock_quality_score",
        ])
        self.assertEqual(block["rows"], [{
            "rank": 1,
            "ticker": "AAPL",
            "current_price": 210.5,
            "strike": 205.0,
            "expiration": "2026-08-21",
            "dte": 30,
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

    def test_ignores_unstructured_or_empty_tool_results(self) -> None:
        self.assertIsNone(table_block_from_tool_result("scan_put_opportunities", "not json"))
        self.assertIsNone(table_block_from_tool_result("scan_put_opportunities", json.dumps({"opportunities": []})))
