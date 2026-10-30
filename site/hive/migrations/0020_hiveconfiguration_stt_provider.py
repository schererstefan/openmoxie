from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('hive', '0019_singlepromptchat_vendor_explicit'),
    ]

    operations = [
        migrations.AddField(
            model_name='hiveconfiguration',
            name='stt_provider',
            field=models.CharField(choices=[('openai', 'Whisper (OpenAI)'), ('muse_transcribe', 'Muse Transcribe (Meta)')], default='openai', max_length=32),
        ),
        migrations.AddField(
            model_name='hiveconfiguration',
            name='muse_transcribe_api_key',
            field=models.TextField(blank=True, default='', null=True),
        ),
    ]
