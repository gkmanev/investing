from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0035_symbol_technical_score"),
    ]

    operations = [
        migrations.AddField(
            model_name="symbol",
            name="option_iv",
            field=models.DecimalField(
                blank=True, decimal_places=4, max_digits=10, null=True
            ),
        ),
        migrations.AddField(
            model_name="symbol",
            name="option_volume",
            field=models.BigIntegerField(blank=True, null=True),
        ),
    ]
