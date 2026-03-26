from io import StringIO
from pathlib import Path
import tempfile

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from api.models import Symbol


class ImportScoredSymbolsCommandTests(TestCase):
    def test_imports_scores_from_csv(self) -> None:
        aapl = Symbol.objects.create(ticker="AAPL", score=80)
        msft = Symbol.objects.create(ticker="MSFT", score=70)
        Symbol.objects.create(ticker="TSLA")

        with tempfile.TemporaryDirectory() as temp_dir:
            csv_path = Path(temp_dir) / "scored_symbols.csv"
            csv_path.write_text(
                "ticker,score\n"
                "AAPL,91\n"
                "MSFT,70\n"
                "NVDA,88\n",
                encoding="utf-8",
            )
            stdout = StringIO()

            result = call_command(
                "import_scored_symbols",
                input=str(csv_path),
                stdout=stdout,
            )

        self.assertEqual(result, str(csv_path))
        aapl.refresh_from_db()
        msft.refresh_from_db()

        self.assertEqual(aapl.score, 91)
        self.assertEqual(msft.score, 70)
        self.assertIn("Updated 1 symbols", stdout.getvalue())
        self.assertIn("Unchanged: 1", stdout.getvalue())
        self.assertIn("Missing: 1", stdout.getvalue())

    def test_dry_run_does_not_write_changes(self) -> None:
        symbol = Symbol.objects.create(ticker="AAPL", score=80)

        with tempfile.TemporaryDirectory() as temp_dir:
            csv_path = Path(temp_dir) / "scored_symbols.csv"
            csv_path.write_text("ticker,score\nAAPL,95\n", encoding="utf-8")
            stdout = StringIO()

            call_command(
                "import_scored_symbols",
                input=str(csv_path),
                dry_run=True,
                stdout=stdout,
            )

        symbol.refresh_from_db()
        self.assertEqual(symbol.score, 80)
        self.assertIn("Would update 1 symbols", stdout.getvalue())

    def test_rejects_invalid_score(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            csv_path = Path(temp_dir) / "scored_symbols.csv"
            csv_path.write_text("ticker,score\nAAPL,not-a-number\n", encoding="utf-8")

            with self.assertRaisesMessage(
                CommandError,
                "Row 2: invalid score 'not-a-number' for AAPL.",
            ):
                call_command("import_scored_symbols", input=str(csv_path))
