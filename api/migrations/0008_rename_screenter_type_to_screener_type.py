from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("api", "0007_investment_screenter_type"),
    ]

    operations = [
        migrations.RenameField(
            model_name="investment",
            old_name="screenter_type",
            new_name="screener_type",
        ),
    ]
