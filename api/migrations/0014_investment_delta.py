from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("api", "0013_investment_rsi"),
    ]

    operations = [
        migrations.AddField(
            model_name="investment",
            name="delta",
            field=models.DecimalField(
                blank=True, decimal_places=2, max_digits=5, null=True
            ),
        ),
    ]
