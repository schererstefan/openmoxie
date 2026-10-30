from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('hive', '0018_muse_spark_chat_provider'),
    ]

    operations = [
        migrations.AddField(
            model_name='singlepromptchat',
            name='vendor_explicit',
            field=models.BooleanField(default=False),
        ),
    ]
