from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0027_symbol_initial_suitability'),
    ]

    operations = [
        migrations.AddField(
            model_name='symbol',
            name='score',
            field=models.IntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='symbol',
            name='classification',
            field=models.CharField(blank=True, max_length=100, null=True),
        ),
    ]
