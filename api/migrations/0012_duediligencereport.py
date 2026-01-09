from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0011_financialstatement"),
    ]

    operations = [
        migrations.CreateModel(
            name="DueDiligenceReport",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("symbol", models.CharField(db_index=True, max_length=16)),
                ("rating", models.CharField(db_index=True, max_length=16)),
                ("confidence", models.FloatField(blank=True, null=True)),
                (
                    "model_name",
                    models.CharField(blank=True, default="", max_length=64),
                ),
                ("report", models.JSONField()),
                ("financial_data", models.JSONField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "ordering": ["-created_at", "symbol"],
            },
        ),
    ]
