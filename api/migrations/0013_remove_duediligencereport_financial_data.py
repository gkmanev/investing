from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0012_duediligencereport"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="duediligencereport",
            name="financial_data",
        ),
    ]
