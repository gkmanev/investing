from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0041_premiumsubscription"),
    ]

    operations = [
        migrations.RenameField(
            model_name="premiumsubscription",
            old_name="paddle_subscription_id",
            new_name="stripe_subscription_id",
        ),
        migrations.RenameField(
            model_name="premiumsubscription",
            old_name="paddle_customer_id",
            new_name="stripe_customer_id",
        ),
    ]
