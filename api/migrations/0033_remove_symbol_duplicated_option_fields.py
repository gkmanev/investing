from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0032_symbol_roi"),
    ]

    operations = [
        migrations.RemoveField(model_name="symbol", name="strike_price"),
        migrations.RemoveField(model_name="symbol", name="bid"),
        migrations.RemoveField(model_name="symbol", name="ask"),
        migrations.RemoveField(model_name="symbol", name="mid"),
    ]
