from decimal import Decimal, InvalidOperation

from django.db import migrations, models


def map_existing_technical_score(apps, schema_editor):
    Symbol = apps.get_model("api", "Symbol")

    def map_score(value):
        if value in (None, ""):
            return None
        try:
            numeric = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return str(value)
        if numeric >= Decimal("0.5"):
            return "Strong Buy"
        if numeric >= Decimal("0.1"):
            return "Buy"
        if numeric > Decimal("-0.1"):
            return "Neutral"
        if numeric > Decimal("-0.5"):
            return "Sell"
        return "Strong Sell"

    for symbol in Symbol.objects.exclude(technical_score__isnull=True):
        mapped = map_score(symbol.technical_score)
        if mapped != symbol.technical_score:
            symbol.technical_score = mapped
            symbol.save(update_fields=["technical_score"])


class Migration(migrations.Migration):
    dependencies = [
        ("api", "0038_dailybriefedition_dailybriefsubscription"),
    ]

    operations = [
        migrations.AlterField(
            model_name="symbol",
            name="technical_score",
            field=models.CharField(
                blank=True,
                choices=[
                    ("Strong Buy", "Strong Buy"),
                    ("Buy", "Buy"),
                    ("Neutral", "Neutral"),
                    ("Sell", "Sell"),
                    ("Strong Sell", "Strong Sell"),
                ],
                max_length=16,
                null=True,
            ),
        ),
        migrations.RunPython(
            map_existing_technical_score,
            migrations.RunPython.noop,
        ),
    ]
