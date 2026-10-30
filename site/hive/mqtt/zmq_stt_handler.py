from .moxie_zmq_handler import ZMQHandler
from .protos.embodied.perception.audio.zmqSTT_pb2 import zmqSTTRequest,zmqSTTResponse
import soundfile as sf
import numpy as np
import io
import time
import logging
import concurrent.futures
import threading
from .ai_factory import create_openai, get_muse_transcribe_key, resolve_stt_provider_detailed
from .muse_transcribe import MuseTranscribeError, transcribe_wav

LOG_WAV=False
OPENAI_MODEL='whisper-1'
STT_PROVIDER_MUSE_TRANSCRIBE = 'muse_transcribe'

logger = logging.getLogger(__name__)

def now_ms():
    return time.time_ns() // 1_000_000


'''
An STT Session is a stream of contiguous audio coming out of the Robot's voice activity detector (VAD). This
is a very simple implementation tuned to whole-utterance transcription: the
existing OpenAI Whisper path and the opt-in Muse Transcribe one-shot path
both transcribe the finished utterance, so we simply accumulate the audio
frames, then transcribe them when complete. Streaming (realtime) STT is a
follow-up and is not implemented here.
'''
class STTSession:
    def __init__(self, parent, device_id, session_id):
        self._parent = parent
        self._device_id = device_id
        self._session_id = session_id
        self._stream_bytes = bytearray()
        self._start_ts = None
        # STT result provenance, surfaced without a proto field: the
        # zmqSTTResponse schema has no fell_back flag, so the session
        # itself records which provider answered and whether Whisper
        # was a fallback. Tests assert on these; logs never carry the
        # transcript or any key.
        self.fell_back = False
        self.stt_provider = 'openai'
        self.stt_source = 'openai_default'

    def on_request(self, req):
        # future ref, this is technically wrong in the design, this ts is realtime on robot, not audio timestamp
        if not self._start_ts:
            self._start_ts = req.timestamp
        self._stream_bytes += req.audio_content
        return len(self._stream_bytes)

    def _transcribe(self, wav_bytes):
        """Transcribe one utterance, returning (text, start_ms, end_ms, fell_back, provider).

        Provider selection is independent of chat: default Whisper-1,
        opt-in Muse Transcribe via ``stt_provider``. Transcribe is
        attempted first when selected; on ANY Transcribe failure
        (auth, network, endpoint error, bad WAV format, empty
        transcript) the utterance falls back to Whisper-1 and
        ``fell_back`` is True. A Whisper failure still raises, so the
        caller sends the usual error response -- a voice turn is never
        dropped silently.
        """
        try:
            provider, fell_back, source = resolve_stt_provider_detailed()
        except ValueError:
            logger.warning('Unknown STT provider configured; falling back to Whisper-1')
            provider, fell_back, source = 'openai', True, 'whisper_fallback_unknown'
        if provider == STT_PROVIDER_MUSE_TRANSCRIBE:
            try:
                result = transcribe_wav(wav_bytes, get_muse_transcribe_key())
                if result.text and result.text.strip():
                    return result.text, result.start_ms, result.end_ms, fell_back, provider
                logger.warning('Muse Transcribe returned an empty transcript; falling back to Whisper-1')
            except MuseTranscribeError as e:
                # Status/kind only: never the transcript, never the key.
                logger.warning('Muse Transcribe failed (status=%s); falling back to Whisper-1', e.status)
            except Exception as e:
                logger.warning('Muse Transcribe failed (%s); falling back to Whisper-1', type(e).__name__)
            fell_back = True
        client = self._parent.get_stt_client()
        transcript = client.audio.transcriptions.create(
            file=('test.wav', wav_bytes),
            model=OPENAI_MODEL,
            response_format="verbose_json",
            timestamp_granularities=["segment"])
        segments = transcript.segments or []
        min_start = min(d.start for d in segments) if segments else 0
        max_end = max(d.end for d in segments) if segments else 0
        return transcript.text, int(min_start * 1000), int(max_end * 1000), fell_back, 'openai'
    
    def perform(self):
        logger.info(f'Processing session_id {self._session_id} with {len(self._stream_bytes)} bytes')
        buffer = io.BytesIO()
        sf.write(
            buffer,  # File-like object (None for bytes)
            np.frombuffer(self._stream_bytes, dtype=np.int16),
            16000,
            format='WAV',
            subtype='PCM_16'  # 16-bit PCM
            )
        wav_bytes = buffer.getvalue()
        # Create proto response, send regardless
        resp = zmqSTTResponse()
        resp.uuid = self._session_id
        resp.type = resp.ResponseType.FINAL
        resp.timestamp = now_ms()

        try:
            text, start_off_ms, end_off_ms, fell_back, provider = self._transcribe(wav_bytes)
            resp.speech = text
            # Moxie needs phrase boundaries, not an alignment for every word.
            resp.start_timestamp = self._start_ts + start_off_ms
            resp.end_timestamp = self._start_ts + end_off_ms
            self.fell_back = fell_back
            self.stt_provider = provider
            self.stt_source = 'muse_transcribe' if provider == STT_PROVIDER_MUSE_TRANSCRIBE else ('whisper_fallback' if fell_back else 'openai_default')
            logger.info(f'STT-FINAL: {text}')
            if fell_back:
                logger.warning(f'STT-FALLBACK: provider={provider} fell_back=True')
        except Exception as e:
            logger.warning(f'Exception handling openAI request: {e}')
            resp.error_code = 66
            resp.error_message = str(e)

        # send response to device
        self._parent.zmq_reply(self._device_id, resp)

        if LOG_WAV:
            logfile = f'{self._session_id}.wav'
            with open(logfile, 'wb') as f:
                f.write(wav_bytes)
                logger.info(f'Wrote WAV data to {logfile}')

'''
This is the handler for all Speech data packets.  By default, the Robot uses stt:4, which begins sending
audio data during session to be transcribed.  If Robot is using stt:0, no STT packets will arrive here.
This is also very simple.  We create unique sessions for each device_id / session pair, pass them all the
data inline, and when a session hits end-of-speech, queue transcription to run in the background.
'''
class STTHandler(ZMQHandler):

    def __init__(self, server):
        super().__init__(server)
        self._sessions = {}
        self._client_state = threading.local()
        self._worker_queue = concurrent.futures.ThreadPoolExecutor(max_workers=5)

    def get_stt_client(self):
        # Reuse connections within each worker, without sharing clients across threads.
        client = getattr(self._client_state, "client", None)
        if client is None:
            client = create_openai()
            self._client_state.client = client
        return client

    def handle_zmq(self, device_id, protoname, protodata):
        req = zmqSTTRequest()
        req.ParseFromString(protodata)
        sesskey = ( device_id, req.uuid )
        if sesskey not in self._sessions:
            self._sessions[sesskey] = STTSession(self, sesskey[0], sesskey[1])
        total_sess_bytes = self._sessions[sesskey].on_request(req)
        # every time we reach EOS, we background it for work
        logger.debug(f'ZMQ Speech VAD: {req.vad} TotalBytes: {total_sess_bytes}')
        if req.vad == req.VADState.END_OF_SPEECH:
            logger.info(f'Session reached END OF SPEECH')
            # session is done, do the work
            sess = self._sessions.pop(sesskey)
            self._worker_queue.submit(sess.perform)
