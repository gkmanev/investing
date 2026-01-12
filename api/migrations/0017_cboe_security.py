from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("api", "0016_merge_0013_0015"),
    ]

    operations = [
        migrations.CreateModel(
            name="CboeSecurity",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("symbol", models.CharField(max_length=25, unique=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "ordering": ["symbol"],
                "db_table": "cboe_securities",
            },
        ),
    ]
