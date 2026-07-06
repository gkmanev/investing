from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0045_agentrun_used_tools_json"),
    ]

    operations = [
        migrations.CreateModel(
            name="SymbolExpirationSnapshot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("expiration_date", models.DateField(db_index=True)),
                ("dte", models.PositiveIntegerField()),
                ("option_volume", models.BigIntegerField(blank=True, null=True)),
                ("option_iv", models.DecimalField(blank=True, decimal_places=4, max_digits=10, null=True)),
                ("roi", models.DecimalField(blank=True, decimal_places=4, max_digits=7, null=True)),
                ("option_data", models.JSONField(blank=True, null=True)),
                ("put_data", models.JSONField(blank=True, null=True)),
                ("call_data", models.JSONField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "symbol",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="expiration_snapshots",
                        to="api.symbol",
                    ),
                ),
            ],
            options={
                "ordering": ["symbol__ticker", "expiration_date"],
            },
        ),
        migrations.AddConstraint(
            model_name="symbolexpirationsnapshot",
            constraint=models.UniqueConstraint(
                fields=("symbol", "expiration_date"),
                name="unique_symbol_expiration_snapshot",
            ),
        ),
    ]
