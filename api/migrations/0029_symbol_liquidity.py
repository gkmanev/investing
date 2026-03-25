from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0028_symbol_score_classification'),
    ]

    operations = [
        migrations.AddField(
            model_name='symbol',
            name='liquidity',
            field=models.CharField(
                blank=True,
                choices=[('GOOD', 'Good'), ('WEAK', 'Weak')],
                max_length=10,
                null=True,
            ),
        ),
    ]
