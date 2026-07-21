from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0046_symbolexpirationsnapshot"),
    ]

    operations = [
        migrations.AddField(
            model_name="agentrun",
            name="llm_usage_json",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="agentrun",
            name="llm_usage_summary_json",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
