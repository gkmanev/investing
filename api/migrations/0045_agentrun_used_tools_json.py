from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0044_agentrun"),
    ]

    operations = [
        migrations.AddField(
            model_name="agentrun",
            name="used_tools_json",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
