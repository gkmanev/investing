from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0023_investment_option_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="investment",
            name="option_data",
            field=models.JSONField(blank=True, null=True),
        ),
    ]
