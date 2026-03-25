from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0033_remove_symbol_duplicated_option_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="symbol",
            name="dcf",
            field=models.DecimalField(
                blank=True, decimal_places=4, max_digits=20, null=True
            ),
        ),
    ]
