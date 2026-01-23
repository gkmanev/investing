from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("api", "0018_alter_screenerfilter_label"),
    ]

    operations = [
        migrations.AddField(
            model_name="investment",
            name="bid_ask_spread",
            field=models.DecimalField(
                blank=True, decimal_places=4, max_digits=10, null=True
            ),
        ),
    ]
