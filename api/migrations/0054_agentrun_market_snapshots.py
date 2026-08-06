# Generated manually for immutable market-data snapshots.

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("api", "0053_agentconversation")]

    operations = [
        migrations.AddField(
            model_name="agentrun",
            name="data_as_of",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="agentrun",
            name="data_source_status",
            field=models.CharField(blank=True, default="", max_length=24),
        ),
        migrations.AddField(
            model_name="agentrun",
            name="refresh_of",
            field=models.ForeignKey(blank=True, db_index=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="refreshes", to="api.agentrun"),
        ),
    ]
