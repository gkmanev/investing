from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0034_symbol_dcf"),
    ]

    operations = [
        migrations.AddField(
            model_name="symbol",
            name="technical_score",
            field=models.DecimalField(
                blank=True, decimal_places=2, max_digits=5, null=True
            ),
        ),
    ]
