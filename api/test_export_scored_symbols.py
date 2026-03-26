import csv
from decimal import Decimal
from io import StringIO
from pathlib import Path
import tempfile

from django.core.management import call_command
from rest_framework.test import APITestCase

from api.models import Symbol


class ExportScoredSymbolsCommandTests(APITestCase):
    def test_exports_only_symbols_with_score(self) -> None:
        Symbol.objects.create(
            ticker="MSFT",
            exchange="NASDAQ",
            market_cap=Decimal("2500000000000.00"),
            score=91,
            classification="Great",
            technical_score=Decimal("88.50"),
        )
        Symbol.objects.create(
            ticker="AAPL",
            exchange="NASDAQ",
            market_cap=Decimal("3000000000000.00"),
            score=85,
            classification="Strong",
        )
        Symbol.objects.create(
            ticker="TSLA",
            exchange="NASDAQ",
            market_cap=Decimal("800000000000.00"),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "exports" / "scored_symbols.csv"
            stdout = StringIO()

            result = call_command(
                "export_scored_symbols",
                output=str(output_path),
                stdout=stdout,
            )

            self.assertEqual(result, str(output_path))
            self.assertTrue(output_path.exists())

            with output_path.open(newline="", encoding="utf-8") as csv_file:
                rows = list(csv.DictReader(csv_file))

        self.assertEqual([row["ticker"] for row in rows], ["AAPL", "MSFT"])
        self.assertEqual(rows[0]["score"], "85")
        self.assertEqual(rows[1]["technical_score"], "88.50")
        self.assertIn("Exported 2 scored symbols", stdout.getvalue())
