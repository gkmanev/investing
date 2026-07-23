# Generated manually for anonymous IP and device-fingerprint trial tracking.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0049_agentrun_anonymous_session"),
    ]

    operations = [
        migrations.AlterField(
            model_name="agentrun",
            name="anonymous_session_key",
            field=models.CharField(blank=True, db_index=True, default="", max_length=64),
        ),
    ]
