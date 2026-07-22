from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [("api", "0047_agentrun_llm_usage")]

    operations = [
        migrations.AddField(
            model_name="agentrun",
            name="result_blocks_json",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
