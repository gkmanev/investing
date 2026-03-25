from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0026_symbol'),
    ]

    operations = [
        migrations.AddField(
            model_name='symbol',
            name='initial_suitability',
            field=models.BooleanField(blank=True, default=None, null=True),
        ),
    ]
