from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0029_symbol_liquidity'),
    ]

    operations = [
        migrations.AddField(
            model_name='symbol',
            name='price',
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=20, null=True),
        ),
        migrations.AddField(
            model_name='symbol',
            name='rsi',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=5, null=True),
        ),
        migrations.AddField(
            model_name='symbol',
            name='option_exp',
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='symbol',
            name='next_earnings_date',
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='symbol',
            name='strike_price',
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=20, null=True),
        ),
        migrations.AddField(
            model_name='symbol',
            name='seeking_alpha_ticker_id',
            field=models.IntegerField(blank=True, null=True),
        ),
    ]
