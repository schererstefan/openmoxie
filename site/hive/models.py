from enum import Enum
from django.db import models
from django.core.validators import validate_comma_separated_integer_list
from django.core.exceptions import ValidationError

class AIVendor(Enum):
    OPEN_AI = 1
    MUSE_SPARK = 2

# Global chat provider choices. Values mirror hive.mqtt.ai_factory
# PROVIDER_OPENAI / PROVIDER_MUSE_SPARK; GPT (openai) is always the default.
CHAT_PROVIDER_OPENAI = "openai"
CHAT_PROVIDER_MUSE_SPARK = "muse_spark"
CHAT_PROVIDER_CHOICES = [
    (CHAT_PROVIDER_OPENAI, "GPT (OpenAI)"),
    (CHAT_PROVIDER_MUSE_SPARK, "Muse Spark (Meta)"),
]

# Muse Spark model offered by the setup page. The standard model is the
# default; the contributor variant is an explicit, non-default choice only
# (its conversation content is handled under different data-use terms).
MUSE_SPARK_MODEL_STANDARD = "muse-spark-1.3"
MUSE_SPARK_MODEL_CONTRIBUTOR = "muse-spark-1.3-contributor"
MUSE_SPARK_MODELS = (MUSE_SPARK_MODEL_STANDARD, MUSE_SPARK_MODEL_CONTRIBUTOR)

class SinglePromptChat(models.Model):
    name = models.CharField(max_length=200)
    module_id = models.CharField(max_length=200)
    content_id = models.CharField(max_length=200)
    max_history = models.IntegerField(default=20)
    max_volleys = models.IntegerField(default=9999)
    opener = models.TextField()
    prompt = models.TextField()
    vendor = models.IntegerField(choices=[(tag.value, tag.name) for tag in AIVendor],default=AIVendor.OPEN_AI.value)
    model = models.CharField(max_length=200, default="gpt-6-luna")
    max_tokens = models.IntegerField(default=70)
    temperature = models.FloatField(default=0.5)
    code = models.TextField(null=True, blank=True) # Python code for filter methods
    source_version = models.IntegerField(default=1)
    
    def __str__(self):
        return self.name
    
class MoxieSchedule(models.Model):
    name = models.CharField(max_length=200)
    schedule = models.JSONField()
    source_version = models.IntegerField(default=1)
    
    def __str__(self):
        return self.name

class DevicePermit(Enum):
    UNKNOWN = 1
    PENDING = 2
    ALLOWED = 3

class MoxieDevice(models.Model):
    device_id = models.CharField(max_length=200)
    email = models.EmailField(null=True, blank=True)
    permit = models.IntegerField(choices=[(tag.value, tag.name) for tag in DevicePermit],default=DevicePermit.UNKNOWN.value)
    schedule = models.ForeignKey(MoxieSchedule, on_delete=models.SET_NULL, null=True)
    name = models.CharField(max_length=200, null=True, blank=True)
    last_connect = models.DateTimeField(null=True, blank=True)
    last_disconnect = models.DateTimeField(null=True, blank=True)
    state = models.JSONField(null=True, blank=True)
    state_updated = models.DateTimeField(null=True, blank=True)
    robot_config = models.JSONField(null=True, blank=True)
    robot_settings = models.JSONField(null=True, blank=True)

    def is_paired(self):
        if self.robot_config:
            return not (self.robot_config.get('pairing_status') == 'unpairing')
        return True

    def __str__(self):
        return self.name if self.name else self.device_id

class MoxieLogs(models.Model):
    device = models.ForeignKey(MoxieDevice, on_delete=models.CASCADE)
    timestamp = models.TimeField()
    uid = models.IntegerField()
    tag = models.CharField(max_length=80)
    message = models.TextField()

class HiveConfiguration(models.Model):
    name = models.CharField(max_length=200)
    openai_api_key = models.TextField(null=True, blank=True, default='')
    # Muse Spark is an optional, opt-in *chat* provider; GPT stays default.
    # Storage deliberately matches openai_api_key exactly (nullable plaintext
    # in the local SQLite database). Storage mechanics are intentionally not
    # changed by the Muse Spark work; see the setup-page LAN warning.
    chat_provider = models.CharField(max_length=32, choices=CHAT_PROVIDER_CHOICES, default=CHAT_PROVIDER_OPENAI)
    muse_spark_api_key = models.TextField(null=True, blank=True, default='')
    muse_spark_model = models.CharField(max_length=200, default=MUSE_SPARK_MODEL_STANDARD)
    external_host = models.CharField(max_length=255, null=True, blank=True, default='')
    allow_unverified_bots = models.BooleanField(default=False)
    google_api_key = models.TextField(null=True, blank=True, default='')
    common_config = models.JSONField(null=True, blank=True)
    common_settings = models.JSONField(null=True, blank=True)

    def __str__(self):
        return self.name

class MentorBehavior(models.Model):
    device = models.ForeignKey(MoxieDevice, on_delete=models.CASCADE)
    # Fields for MBH
    module_id = models.CharField(max_length=80, null=True, blank=True)
    content_id = models.CharField(max_length=80, null=True, blank=True)
    content_day = models.CharField(max_length=80, null=True, blank=True)
    timestamp = models.BigIntegerField()
    action = models.CharField(max_length=80, null=True, blank=True)
    instance_id = models.BigIntegerField()
    ended_reason = models.CharField(max_length=80, null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['device', 'timestamp'], name='device_timestamp_idx'),
        ]

    def __str__(self):
        return f'{self.timestamp}-{self.device}-{self.module_id}/{self.content_id}-{self.action}'


class GlobalAction(Enum):
    RESPONSE = 1
    LAUNCH = 2
    CONFIRM_LAUNCH = 3
    METHOD = 4

class GlobalResponse(models.Model):
    name = models.TextField()      # common name
    pattern = models.TextField()   # regex pattern to match speech
    entity_groups = models.CharField(max_length=255, validators=[validate_comma_separated_integer_list], null=True, blank=True)
    action = models.IntegerField(choices=[(tag.value, tag.name) for tag in GlobalAction],default=GlobalAction.RESPONSE.value)
    response_text = models.TextField(null=True, blank=True)  # plaintext response
    response_markup = models.TextField(null=True, blank=True)  # markup override response
    module_id = models.CharField(max_length=80, null=True, blank=True)  # for launches, module ID to target
    content_id = models.CharField(max_length=80, null=True, blank=True) # for launches, content ID to target
    code = models.TextField(null=True, blank=True) # Python code for METHOD, w/ def get_response(request, response, entities):
    sort_key = models.IntegerField(default=1) # in case ordering matters, they order desc so high goes first
    source_version = models.IntegerField(default=1)

    # Ensure we have all we need
    def clean(self):
        if self.action == GlobalAction.METHOD.value and not self.code:
            raise ValidationError({'code': 'Code is required for METHOD action'})
        elif (self.action == GlobalAction.LAUNCH.value or self.action == GlobalAction.CONFIRM_LAUNCH.value) and not self.module_id:
            raise ValidationError({'module_id': 'Module ID is required for LAUNCH actions'})
        elif self.action != GlobalAction.METHOD.value and not self.response_text:
            raise ValidationError({'response_text': 'Response Text is required for actions except METHOD'})
        
    def __str__(self):
        return self.name
    
class PersistentData(models.Model):
    device = models.OneToOneField(MoxieDevice, on_delete=models.CASCADE)
    data = models.JSONField()

    def __str__(self):
        return f'{self.device} - Data'