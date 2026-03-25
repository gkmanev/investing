from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0024_investment_option_data"),
    ]

    operations = [
        migrations.RemoveField(model_name="investment", name="roi"),
        migrations.RemoveField(model_name="investment", name="delta"),
        migrations.RemoveField(model_name="investment", name="bid_ask_spread"),
        migrations.RemoveField(model_name="investment", name="options_suitability"),
        migrations.RemoveField(model_name="investment", name="weekly_options"),
    ]
