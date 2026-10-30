from django.forms import model_to_dict
from django.shortcuts import redirect
from django.urls import reverse
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth import get_user_model
from django.views import generic
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import csrf_exempt
from django.http import Http404, HttpResponseBadRequest, JsonResponse
from django.shortcuts import render, get_object_or_404
from django.http import HttpResponse,HttpResponseRedirect
from django.conf import settings
import qrcode
from PIL import Image
from io import BytesIO

from . import logtail
from .models import CHAT_PROVIDER_MUSE_SPARK, CHAT_PROVIDER_OPENAI, MUSE_SPARK_MODELS, GlobalResponse, SinglePromptChat, MoxieDevice, MoxieSchedule, HiveConfiguration, MentorBehavior
from .content.data import DM_MISSION_CONTENT_IDS, get_moxie_customization_groups
from .data_import import update_import_status, import_content
from .mqtt.moxie_server import get_instance
from .mqtt.control_commands import (
    CONTROL_COMMANDS,
    DANCE_MODULE_ID,
    DEFAULT_LOOK_BEHAVIOUR,
    GESTURE_BEHAVIOURS,
    LOOK_BEHAVIOURS,
    build_dance_payload,
    build_sleep_payload,
    is_dance_enabled,
    parse_duration,
    parse_single_command,
    phonetic_name,
    validate_intensity,
    validate_mood,
)
from .mqtt.robot_data import DEFAULT_ROBOT_CONFIG, DEFAULT_ROBOT_SETTINGS
from .mqtt.stt_knobs import resolve_stt_mode, validate_stt_knobs
from .mqtt.volley import Volley
import json
import uuid
import logging

logger = logging.getLogger(__name__)

# ROOT - Show setup if we have no config record, dashboard otherwise
def root_view(request):
    cfg = HiveConfiguration.objects.filter(name='default')
    if cfg:
        return HttpResponseRedirect(reverse("hive:dashboard"))
    else:
        return HttpResponseRedirect(reverse("hive:setup"))

# SETUP - Edit systemn configuration record
class SetupView(generic.TemplateView):
    template_name = "hive/setup.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        User = get_user_model()
        context['needs_admin'] = not User.objects.filter(is_superuser=True).exists()
        curr_cfg = HiveConfiguration.objects.filter(name='default').first()
        if curr_cfg:
            context['object'] = curr_cfg
        return context

# SETUP-POST - Save system config changes
@require_http_methods(["POST"])
def hive_configure(request):
    cfg, created = HiveConfiguration.objects.get_or_create(name='default')
    openai = request.POST['apikey']
    if openai:
        cfg.openai_api_key = openai
    google = request.POST['googleapikey']
    if google:
        # Moxie likes compact json, so rewrite json input to be safe
        cfg.google_api_key = json.dumps(json.loads(google))
    # Optional Muse Spark chat provider (additive; blank key means keep).
    chat_provider = request.POST.get('chat_provider')
    if chat_provider in (CHAT_PROVIDER_OPENAI, CHAT_PROVIDER_MUSE_SPARK):
        cfg.chat_provider = chat_provider
    muse_spark_key = request.POST.get('musesparkapikey')
    if muse_spark_key:
        cfg.muse_spark_api_key = muse_spark_key
    muse_spark_model = request.POST.get('muse_spark_model')
    if muse_spark_model in MUSE_SPARK_MODELS:
        cfg.muse_spark_model = muse_spark_model
    # STT knobs (provider + streaming controls) are validated in
    # exactly one place -- stt_knobs.validate_stt_knobs -- so raw POST
    # values never reach storage or any provider API unvalidated.
    # Allowlist fields (provider/endpointing/language) keep the
    # current stored value when rejected, matching the previous
    # provider-only behaviour; timeout/keywords store the
    # clamped/cleaned values. Checkboxes follow form semantics (an
    # unchecked box is absent and saves as off). Nothing here raises
    # on bad input. Transcribe override key: blank means keep (and
    # blank stored means reuse the Muse Spark key).
    clean_knobs, rejected_knobs = validate_stt_knobs({
        'stt_provider': request.POST.get('stt_provider'),
        'stt_streaming_enabled': request.POST.get('stt_streaming_enabled'),
        'stt_diarization_enabled': request.POST.get('stt_diarization_enabled'),
        'stt_endpointing_mode': request.POST.get('stt_endpointing_mode'),
        'stt_endpointing_timeout_ms': request.POST.get('stt_endpointing_timeout_ms'),
        'stt_language_hint': request.POST.get('stt_language_hint'),
        'stt_keyword_bias': request.POST.get('stt_keyword_bias'),
        'stt_code_switching_enabled': request.POST.get('stt_code_switching_enabled'),
    })
    if rejected_knobs:
        # Field names only: raw knob values are not logged.
        logger.warning("Rejected invalid STT knob fields: %s", ",".join(sorted(rejected_knobs)))
    if request.POST.get('stt_provider') is not None and 'stt_provider' not in rejected_knobs:
        cfg.stt_provider = clean_knobs['stt_provider']
    cfg.stt_streaming_enabled = clean_knobs['stt_streaming_enabled']
    cfg.stt_diarization_enabled = clean_knobs['stt_diarization_enabled']
    if request.POST.get('stt_endpointing_mode') is not None and 'stt_endpointing_mode' not in rejected_knobs:
        cfg.stt_endpointing_mode = clean_knobs['stt_endpointing_mode']
    if request.POST.get('stt_endpointing_timeout_ms') is not None:
        cfg.stt_endpointing_timeout_ms = clean_knobs['stt_endpointing_timeout_ms']
    if request.POST.get('stt_language_hint') is not None and 'stt_language_hint' not in rejected_knobs:
        cfg.stt_language_hint = clean_knobs['stt_language_hint']
    if request.POST.get('stt_keyword_bias') is not None:
        cfg.stt_keyword_bias = clean_knobs['stt_keyword_bias']
    cfg.stt_code_switching_enabled = clean_knobs['stt_code_switching_enabled']
    muse_transcribe_key = request.POST.get('musetranscribeapikey')
    if muse_transcribe_key:
        cfg.muse_transcribe_api_key = muse_transcribe_key
    cfg.external_host = request.POST['hostname']
    cfg.allow_unverified_bots = request.POST.get('allowall') == "on"
    # Bootstrap any default data if not present
    if not cfg.common_config:
        cfg.common_config = DEFAULT_ROBOT_CONFIG
    if not cfg.common_settings:
        cfg.common_settings = DEFAULT_ROBOT_SETTINGS
    cfg.save()

    # Create Admin User if data exists and we dont have one
    User = get_user_model()
    if not User.objects.filter(is_superuser=True).exists():
        admin = request.POST.get("adminUser")
        adminPassword = request.POST.get("adminPassword")
        if admin and adminPassword:
            User.objects.create_superuser(admin, None, adminPassword)
            logger.info(f"Created superuser '{admin}'")
        else:
            logger.warning(f"Couldn't create missing superuser")

    logger.info("Updated default Hive Configuration")
    # reload any cached db objects
    get_instance().update_from_database()
    return HttpResponseRedirect(reverse("hive:dashboard"))

# DASHBOARD - View and overview of the system
class DashboardView(generic.TemplateView):
    template_name = "hive/dashboard.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        alert_message = kwargs.get('alert_message', None)
        if alert_message:
            context['alert'] = alert_message
        context['recent_devices'] = MoxieDevice.objects.all()
        context['conversations'] = SinglePromptChat.objects.all()
        context['schedules'] = MoxieSchedule.objects.all()
        context['live'] = get_instance().robot_data().connected_list()
        # Read-only STT summary (knob values only -- never key values;
        # key presence is a boolean used solely for mode resolution).
        hive_config = HiveConfiguration.objects.filter(name='default').first()
        if hive_config:
            has_muse_key = bool(
                str(getattr(hive_config, 'muse_transcribe_api_key', '') or '').strip()
                or str(getattr(hive_config, 'muse_spark_api_key', '') or '').strip())
            provider = getattr(hive_config, 'stt_provider', 'openai') or 'openai'
            streaming_enabled = bool(getattr(hive_config, 'stt_streaming_enabled', False))
            mode, fell_back, _source = resolve_stt_mode(provider, streaming_enabled, has_muse_key)
            context['stt_status'] = {
                'provider': provider,
                'mode': mode,
                'fell_back': fell_back,
                'streaming_enabled': streaming_enabled,
                'diarization_enabled': bool(getattr(hive_config, 'stt_diarization_enabled', False)),
                'endpointing_mode': getattr(hive_config, 'stt_endpointing_mode', 'auto') or 'auto',
                'endpointing_timeout_ms': getattr(hive_config, 'stt_endpointing_timeout_ms', 1000),
                'language_hint': getattr(hive_config, 'stt_language_hint', 'auto') or 'auto',
                'code_switching_enabled': bool(getattr(hive_config, 'stt_code_switching_enabled', False)),
            }
        return context

# INTERACT - Chat with a remote conversation
class InteractionView(generic.DetailView):
    template_name = "hive/interact.html"
    model = SinglePromptChat

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['token'] = uuid.uuid4().hex
        return context

# INTERACT-POST - Handle user input during interact
@require_http_methods(["POST"])
@csrf_exempt
def interact_update(request):
    speech = request.POST['speech']
    token = request.POST['token']
    module_id = request.POST['module_id']
    content_id = request.POST['content_id'].split('|')[0]
    session = get_instance().get_web_session_for_module(token, module_id, content_id)
    volley = Volley.request_from_speech(speech, device_id=token, module_id=module_id, content_id=content_id, local_data=session.local_data)
    # Check global responses manually
    gresp = get_instance().get_web_session_global_response(volley) if speech else None
    if gresp:
        line = gresp
        details = {}
    else:
        session.handle_volley(volley)
        line = volley.debug_response_string()
        details = volley.response
    return JsonResponse({'message': line, 'details': details})

# RELOAD - Reload any records initialized from the database
def reload_database(request):
    get_instance().update_from_database()
    return redirect('hive:dashboard_alert', alert_message='Updated from database.')

# ENDPOINT - Render QR code to migrate Moxie
def endpoint_qr(request):
    img = qrcode.make(get_instance().get_endpoint_qr_data())
    buffer = BytesIO()
    img.save(buffer, 'PNG')
    buffer.seek(0)
    return HttpResponse(buffer, content_type='image/png')

# WIFI EDIT - Edit wifi params to create QR Code
class WifiQREditView(generic.TemplateView):
    template_name = "hive/wifi.html"

# WIFI-POST - Render QR code for Wifi Creds
@require_http_methods(["POST"])
def wifi_qr(request):
    ssid = request.POST['ssid']
    password = request.POST['password']
    band_id = request.POST['frequency']
    hidden = 'hidden' in request.POST
    img = qrcode.make(get_instance().get_wifi_qr_data(ssid, password, band_id, hidden))
    buffer = BytesIO()
    img.save(buffer, 'PNG')
    buffer.seek(0)
    return HttpResponse(buffer, content_type='image/png')

# MOXIE - View Moxie Params and config
class MoxieView(generic.DetailView):
    template_name = "hive/moxie.html"
    model = MoxieDevice

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['active_config'] = get_instance().robot_data().get_config_for_device(self.object)
        context['schedules'] = MoxieSchedule.objects.all()
        return context

# MOXIE-POST - Save changes to a Moxie record
@require_http_methods(["POST"])
def moxie_edit(request, pk):
    try:
        device = MoxieDevice.objects.get(pk=pk)
        # changes to base model
        device.name = request.POST["moxie_name"]
        device.schedule = MoxieSchedule.objects.get(pk=request.POST["schedule"])
        # changes to json field inside config
        if device.robot_config == None:
           # robot_config optional, create a new one to hold these
           device.robot_config = {}
        device.robot_config["screen_brightness"] = float(request.POST["screen_brightness"])
        device.robot_config["audio_volume"] = float(request.POST["audio_volume"])
        if "child_pii" in device.robot_config:
            device.robot_config["child_pii"]["nickname"] = request.POST["nickname"]
        else:
            device.robot_config["child_pii"] = { "nickname": request.POST["nickname"] }
        # pairing/unpairing
        device.robot_config["pairing_status"] = request.POST["pairing_status"]
        device.save()
        get_instance().handle_config_updated(device)
    except MoxieDevice.DoesNotExist as e:
        logger.warning("Moxie update for unfound pk {pk}")
    return HttpResponseRedirect(reverse("hive:dashboard"))

# MOXIE - Edit Moxie Face Customizations
class MoxieFaceView(generic.DetailView):
    template_name = "hive/face.html"
    model = MoxieDevice

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['assets'] = get_moxie_customization_groups()
        context['face_options'] = get_instance().robot_data().get_config_for_device(self.object).get('child_pii', {}).get('face_options', [])
        return context

# FACE-POST - Save changes to a Moxie Face
@require_http_methods(["POST"])
def face_edit(request, pk):
    try:
        device = MoxieDevice.objects.get(pk=pk)
        new_face = []
        for key in request.POST.keys():
            if key.startswith('asset_'):
                val = request.POST[key]
                if val != '--':
                    new_face.append(val)

        if "child_pii" in device.robot_config:
            device.robot_config["child_pii"]["face_options"] = new_face
        else:
            device.robot_config["child_pii"] = { "face_options": new_face }

        # Moxie-Unity keeps a cached record of face textture keyed by the 'id' field.  This
        # Sets a new unique id to invalidate any old/corrupt cached record
        suffix = ''
        if request.POST.get('child_recover'):
            device.robot_config["child_pii"]["id"] = str(uuid.uuid4())
            suffix = " - Created new child ID"

        device.save()
        get_instance().handle_config_updated(device)
        return redirect('hive:dashboard_alert', alert_message=f'Updated face for {device}{suffix}')
    except MoxieDevice.DoesNotExist as e:
        logger.warning("Moxie update for unfound pk {pk}")
        return redirect('hive:dashboard_alert', alert_message='No such Moxie')

# MOXIE - Puppeteer Moxie
class MoxiePuppetView(generic.DetailView):
    template_name = "hive/puppet.html"
    model = MoxieDevice

# PUPPET API - Handle AJAX calls from puppet view (+ Full Control commands).
# Exactly one command per POST. Movement class (gesture/dance/look/sleep) is
# at most one per call by construction: parse_single_command rejects any
# bundled/list command shape. New commands return {'result', 'command', ...};
# existing enable/disable/interrupt/speak keep {'result': True}.
@csrf_exempt
def puppet_api(request, pk):
    try:
        device = MoxieDevice.objects.get(pk=pk)
        if request.method == 'GET':
            # Handle GET request (status; no state change, no publish)
            result = {
                "online": get_instance().robot_data().device_online(device.device_id),
                "puppet_state": get_instance().robot_data().get_puppet_state(device.device_id),
                "puppet_enabled": device.robot_config.get("moxie_mode") == "TELEHEALTH" if device.robot_config else False,
                "dance_enabled": is_dance_enabled(),
                "control_commands": list(CONTROL_COMMANDS),
            }
            return JsonResponse(result)
        elif request.method == 'POST':
            # Handle COMMANDS request
            if not device.robot_config:
                device.robot_config = {}
            try:
                cmd = parse_single_command(request.POST.get('command'), request.POST.keys())
            except ValueError as e:
                return JsonResponse({'result': False, 'error': str(e)}, status=400)
            if cmd == "enable":
                device.robot_config["moxie_mode"] = "TELEHEALTH"
                device.save()
                get_instance().handle_config_updated(device)
            elif cmd == "disable":
                device.robot_config.pop("moxie_mode", None)
                device.save()
                get_instance().handle_config_updated(device)
            elif cmd == "interrupt":
                get_instance().send_telehealth_interrupt(device.device_id)
            elif cmd == "speak":
                speech = request.POST.get('speech', '')
                if not speech:
                    return JsonResponse({'result': False, 'error': 'speech is required'}, status=400)
                try:
                    mood = validate_mood(request.POST.get('mood'))
                    intensity = validate_intensity(request.POST.get('intensity'))
                except ValueError as e:
                    return JsonResponse({'result': False, 'error': str(e)}, status=400)
                get_instance().send_telehealth_speech(device.device_id, phonetic_name(speech),
                                                      mood, intensity)
            elif cmd in ("gesture", "look"):
                behaviour = (request.POST.get('behaviour') or '').strip()
                if cmd == "gesture" and behaviour not in GESTURE_BEHAVIOURS:
                    return JsonResponse({'result': False, 'command': cmd, 'error': 'behaviour not allowlisted'}, status=400)
                if cmd == "look":
                    behaviour = behaviour or DEFAULT_LOOK_BEHAVIOUR
                    if behaviour not in LOOK_BEHAVIOURS:
                        return JsonResponse({'result': False, 'command': cmd, 'error': 'behaviour not allowlisted'}, status=400)
                try:
                    duration = parse_duration(request.POST.get('duration'))
                except ValueError as e:
                    return JsonResponse({'result': False, 'command': cmd, 'error': str(e)}, status=400)
                spoken = phonetic_name(request.POST.get('speech') or '')
                logger.info("Control %s behaviour=%s device=%s", cmd, behaviour, device.device_id)
                get_instance().send_telehealth_behaviour(device.device_id, behaviour, speech=spoken, duration=duration)
                return JsonResponse({'result': True, 'command': cmd, 'behaviour': behaviour})
            elif cmd == "dance":
                try:
                    payload = build_dance_payload(module_id=request.POST.get('module_id'))
                except ValueError as e:
                    return JsonResponse({'result': False, 'command': cmd, 'error': str(e)}, status=400)
                logger.info("Control dance module=%s device=%s", DANCE_MODULE_ID, device.device_id)
                get_instance().send_command_to_bot_json(device.device_id, 'remote_chat', payload)
                return JsonResponse({'result': True, 'command': cmd, 'module_id': DANCE_MODULE_ID})
            elif cmd == "sleep":
                logger.info("Control sleep device=%s", device.device_id)
                get_instance().send_command_to_bot_json(device.device_id, 'remote_chat', build_sleep_payload())
                return JsonResponse({'result': True, 'command': cmd})
            elif cmd == "wake":
                ok = get_instance().send_wakeup_to_bot(device.device_id)
                return JsonResponse({'result': bool(ok), 'command': cmd})
            else:
                return JsonResponse({'result': False, 'error': f'unknown command: {cmd}'}, status=400)
        return JsonResponse({'result': True})
    except MoxieDevice.DoesNotExist as e:
        logger.warning("Moxie puppet speak for unfound pk {pk}")
        return HttpResponseBadRequest()
    
# MOXIE - View Moxie Mission Sets to Complete
class MoxieMissionsView(generic.DetailView):
    template_name = "hive/missions.html"
    model = MoxieDevice

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # list of tupes (key,prettykey)
        context['mission_sets'] = [ (key, key.replace("_", " ")) for key in DM_MISSION_CONTENT_IDS.keys() ]
        return context

# MOXIE-POST - Save changes to a Moxie record
@require_http_methods(["POST"])
def mission_edit(request, pk):
    try:
        device = MoxieDevice.objects.get(pk=pk)

        mission_action = request.POST["mission_action"]
        if mission_action == "reset":
            # Delete all MBH to start fresh
            MentorBehavior.objects.filter(device=device).delete()
            msg = f'Reset ALL progress for {device}'
        else:
            # Handle mission set actions... get all the CIDs for the selected sets
            mission_sets = request.POST.getlist("mission_sets")
            dm_cid_list = [cid for ms in mission_sets for cid in DM_MISSION_CONTENT_IDS.get(ms, [])]
            if mission_action == "forget":
                # Delete any records with these module/content ID (completed, quit)
                MentorBehavior.objects.filter(device=device, module_id='DM', content_id__in=dm_cid_list).delete()
                msg = f'Forgot {len(mission_sets)} Daily Mission Sets ({len(dm_cid_list)} missions) for {device}'
            else: # == "complete"
                # Create new completions for all these mission content IDs
                get_instance().robot_data().add_mbh_completion_bulk(device.device_id, module_id="DM", content_id_list=dm_cid_list)
                msg = f'Completed {len(mission_sets)} Daily Mission Sets ({len(dm_cid_list)} missions) for {device}'

        return redirect('hive:dashboard_alert', alert_message=msg)
    except MoxieDevice.DoesNotExist as e:
        logger.warning("Moxie update for unfound pk {pk}")
        return redirect('hive:dashboard_alert', alert_message='No such Moxie')

# WAKE UP A MOXIE THAT IS USING WAKE BUTTON
def moxie_wake(request, pk):
    try:
        device = MoxieDevice.objects.get(pk=pk)
        logger.info(f'Waking up {device}')
        alert_msg = "Wake message sent!" if get_instance().send_wakeup_to_bot(device.device_id) else 'Moxie was offline.'
        return redirect('hive:dashboard_alert', alert_message=alert_msg)
    except MoxieDevice.DoesNotExist as e:
        logger.warning("Moxie wake for unfound pk {pk}")
        return redirect('hive:dashboard_alert', alert_message='No such Moxie')

# MOXIE - Export Moxie Content Data - Selection View
class ExportDataView(generic.TemplateView):
    template_name = "hive/export.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['conversations'] = SinglePromptChat.objects.all()
        context['schedules'] = MoxieSchedule.objects.all()
        context['globals'] = GlobalResponse.objects.all()
        return context
    
# MOXIE - Export Moxie Content Data - Save Action
@require_http_methods(["POST"])
def export_data(request):
    content_name = request.POST['content_name']
    content_details = request.POST['content_details']
    globals = request.POST.getlist("globals")
    schedules = request.POST.getlist("schedules")
    conversations = request.POST.getlist("conversations")
    if not content_name:
        content_name = 'moxie_content'
    output = { "name": content_name, "details": content_details }
    for pk in globals:
        r = GlobalResponse.objects.get(pk=pk)
        rec = model_to_dict(r, exclude=['id'])
        output["globals"] = output.get("globals", []) + [rec]
    for pk in schedules:
        r = MoxieSchedule.objects.get(pk=pk)
        rec = model_to_dict(r, exclude=['id'])
        output["schedules"] = output.get("schedules", []) + [rec]
    for pk in conversations:
        r = SinglePromptChat.objects.get(pk=pk)
        rec = model_to_dict(r, exclude=['id'])
        output["conversations"] = output.get("conversations", []) + [rec]
    # Save output as JSON file
    response = JsonResponse(output, json_dumps_params={'indent': 4})
    response['Content-Disposition'] = f'attachment; filename="{content_name}.json"'
    return response

# MOXIE - Import Moxie Content Data
@require_http_methods(['POST'])
def upload_import_data(request):
    json_file = request.FILES.get('json_file')
    if not json_file:
        return JsonResponse({'error': 'No file uploaded'}, status=400)

    try:
        json_data = json.loads(json_file.read().decode('utf-8'))
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON file'}, status=400)

    # Preprocess the JSON data to build the context for the template
    update_import_status(json_data)
    context = {
        'json_data': json_data,
        'json_data_str': json.dumps(json_data)
        # Add other context variables as needed
    }
    return render(request, 'hive/import.html', context)

@require_http_methods(['POST'])
def import_data(request):
    # these hold indexes into the source JSON arrays that we want to import
    g_list = request.POST.getlist("globals")
    s_list = request.POST.getlist("schedules")
    c_list = request.POST.getlist("conversations")
    # the original JSON upload, passed back to us
    jstring = request.POST.get("json_data")
    logger.info(f'IMPORTING {jstring}')
    json_data = json.loads(jstring)
    # finally import the data
    message = import_content(json_data, g_list, s_list, c_list)
    # and refresh all things
    get_instance().update_from_database()
    return redirect('hive:dashboard_alert', alert_message=message)

# MOXIE - View Moxie Data
class MoxieDataView(generic.DetailView):
    template_name = "hive/moxie_data.html"
    model = MoxieDevice

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['active_config'] = json.dumps(get_instance().robot_data().get_config_for_device(self.object))
        context['persist_data'] = json.dumps(get_instance().robot_data().get_persist_for_device(self.object))
        return context

# LOGS - Read-only tail of the server log (debug.log) and MQTT broker log
# (mosquitto.log) from the app work dir. Admin-gated: staff_member_required
# is the existing Django admin login gate (the only auth gate in this
# project); these GET views deliberately have no CSRF exemption. Only the two
# fixed filenames in logtail.ALLOWED_LOG_FILES can ever be read - any other
# file parameter is rejected, and no path is ever taken from the request.
def _logs_payload(request):
    params = request.GET
    raw_count = next(
        (params[key] for key in ("lines", "n", "count", "limit") if params.get(key)),
        None)
    count = logtail.parse_line_count(raw_count)
    filename = params.get("file") or params.get("filename") or None
    if filename is not None and filename not in logtail.ALLOWED_LOG_FILES:
        raise ValueError(f"log file not allowed: {filename!r}")
    filters = {
        "device": params.get("device") or params.get("device_id") or "",
        "tag": params.get("tag") or "",
        "level": params.get("level") or "",
        "q": params.get("q") or params.get("search") or params.get("text") or "",
        "file": filename or "",
    }
    files = logtail.collect_logs(
        filenames=[filename] if filename else None,
        count=count,
        device=filters["device"], tag=filters["tag"],
        level=filters["level"], q=filters["q"])
    return {
        "files": files,
        "filters": filters,
        "lines_requested": count,
        "allowed_files": list(logtail.ALLOWED_LOG_FILES),
    }

@staff_member_required(login_url="/admin/login/")
@require_http_methods(["GET"])
def logs_view(request):
    try:
        payload = _logs_payload(request)
    except ValueError as e:
        return HttpResponseBadRequest(str(e))
    context = dict(payload)
    context["files_data"] = list(payload["files"].values())
    return render(request, "hive/logs.html", context)

@staff_member_required(login_url="/admin/login/")
@require_http_methods(["GET"])
def logs_api(request):
    try:
        payload = _logs_payload(request)
    except ValueError as e:
        return JsonResponse({"error": str(e)}, status=400)
    return JsonResponse(payload)