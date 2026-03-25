from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0022_investment_next_earnings_date"),
    ]

    operations = [
        # Drop unique constraint on ticker
        migrations.AlterField(
            model_name="investment",
            name="ticker",
            field=models.CharField(max_length=50),
        ),
        # Widen delta to 4 decimal places
        migrations.AlterField(
            model_name="investment",
            name="delta",
            field=models.DecimalField(
                blank=True, decimal_places=4, max_digits=7, null=True
            ),
        ),
        # New fields
        migrations.AddField(
            model_name="investment",
            name="option_symbol",
            field=models.CharField(blank=True, max_length=50, null=True, unique=True),
        ),
        migrations.AddField(
            model_name="investment",
            name="bid",
            field=models.DecimalField(
                blank=True, decimal_places=4, max_digits=10, null=True
            ),
        ),
        migrations.AddField(
            model_name="investment",
            name="ask",
            field=models.DecimalField(
                blank=True, decimal_places=4, max_digits=10, null=True
            ),
        ),
        migrations.AddField(
            model_name="investment",
            name="mid",
            field=models.DecimalField(
                blank=True, decimal_places=4, max_digits=10, null=True
            ),
        ),
    ]
