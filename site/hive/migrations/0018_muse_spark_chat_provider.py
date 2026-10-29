from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('hive', '0017_alter_singlepromptchat_model_default'),
    ]

    operations = [
        migrations.AddField(
            model_name='hiveconfiguration',
            name='chat_provider',
            field=models.CharField(choices=[('openai', 'GPT (OpenAI)'), ('muse_spark', 'Muse Spark (Meta)')], default='openai', max_length=32),
        ),
        migrations.AddField(
            model_name='hiveconfiguration',
            name='muse_spark_api_key',
            field=models.TextField(blank=True, default='', null=True),
        ),
        migrations.AddField(
            model_name='hiveconfiguration',
            name='muse_spark_model',
            field=models.CharField(default='muse-spark-1.3', max_length=200),
        ),
        migrations.AlterField(
            model_name='singlepromptchat',
            name='vendor',
            field=models.IntegerField(choices=[(1, 'OPEN_AI'), (2, 'MUSE_SPARK')], default=1),
        ),
    ]
