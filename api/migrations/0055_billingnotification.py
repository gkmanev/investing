# Generated manually for paid Stripe invoice notification de-duplication.

from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("api", "0054_agentrun_market_snapshots"),
    ]

    operations = [
        migrations.CreateModel(
            name="BillingNotification",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("stripe_invoice_id", models.CharField(max_length=255, unique=True)),
                ("sent_at", models.DateTimeField(auto_now_add=True)),
                ("user", models.ForeignKey(on_delete=models.deletion.CASCADE, to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-sent_at"]},
        ),
    ]
