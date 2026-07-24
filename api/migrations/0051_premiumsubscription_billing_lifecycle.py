# Generated manually for Stripe subscription lifecycle display fields.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0050_agentrun_anonymous_identity_key"),
    ]

    operations = [
        migrations.AddField(
            model_name="premiumsubscription",
            name="cancel_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="premiumsubscription",
            name="cancel_at_period_end",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="premiumsubscription",
            name="canceled_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="premiumsubscription",
            name="current_period_start",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="premiumsubscription",
            name="ended_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="premiumsubscription",
            name="start_date",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
