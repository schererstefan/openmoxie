from .moxie_zmq_handler import ZMQHandler
from .protos.embodied.perception.audio.zmqSTT_pb2 import zmqSTTRequest,zmqSTTResponse
import soundfile as sf
import numpy as np
import io
import time
import logging
import concurrent.futures
import threading
from .ai_factory import create_openai, get_muse_transcribe_key, get_stt_provider, get_stt_streaming_config, get_stt_streaming_enabled, has_muse_transcribe_key
from .muse_transcribe import MuseTranscribeError, transcribe_wav
from .muse_transcribe_streaming import StreamingError, StreamingSession
from .stt_knobs import resolve_stt_mode

LOG_WAV=False
OPENAI_MODEL='whisper-1'
STT_PROVIDER_MUSE_TRANSCRIBE = 'muse_transcribe'
STT_PROVIDER_MUSE_STREAMING = 'muse_streaming'

logger = logging.getLogger(__name__)

def now_ms():
    return time.time_ns() // 1_000_000


'''
An STT Session is a stream of contiguous audio coming out of the Robot's voice activity detector (VAD). This
is a very simple implementation tuned to whole-utterance transcription: the
existing OpenAI Whisper path and the opt-in Muse Transcribe one-shot path
both transcribe the finished utterance, so we simply accumulate the audio
frames, then transcribe them when complete. Opt-in Muse streaming (80 ms
chunks via muse_transcribe_streaming) is attempted first only when the
effective mode is muse_streaming; partials are never forwarded to the robot,
an early final replies immediately, and any streaming failure falls back to
the one-shot path and then Whisper -- a voice turn is never dropped silently.
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
        # Streaming state. _streaming_attempted also suppresses WAV
        # logging (streaming turns ignore LOG_WAV) and transcript-
        # content logging for the turn. Timings are durations only.
        self._streaming_attempted = False
        self._live_stream = None
        self._live_stream_failed = False
        self.stream_first_partial_ms = None
        self.stream_final_ms = None
        self.stream_endpoint_by = None

    def on_request(self, req):
        # future ref, this is technically wrong in the design, this ts is realtime on robot, not audio timestamp
        if not self._start_ts:
            self._start_ts = req.timestamp
        self._stream_bytes += req.audio_content
        return len(self._stream_bytes)

    def _effective_mode(self):
        """(mode, fell_back, source) from ai_factory + stt_knobs."""
        return resolve_stt_mode(
            get_stt_provider(), get_stt_streaming_enabled(), has_muse_transcribe_key())

    def _new_stream(self):
        factory = getattr(self._parent, 'streaming_websocket_factory', None)
        return StreamingSession(
            get_stt_streaming_config(), websocket_factory=factory,
            api_key=get_muse_transcribe_key())

    def _record_stream_timings(self, stream):
        self.stream_first_partial_ms = stream.first_partial_ms
        self.stream_final_ms = stream.final_ms
        self.stream_endpoint_by = stream.endpoint_by

    def _try_streaming(self, pcm_bytes):
        """Stream buffered PCM as 80 ms chunks; return the final text.

        With no factory configured on the handler this uses the lazy
        real-transport adapter, which raises StreamingUnavailable in
        this offline branch (no websocket library / unverified live
        handshake) -- that is correct and documented, not hidden: the
        caller logs the kind and falls back to one-shot, then Whisper.
        """
        stream = self._new_stream()
        try:
            stream.connect()
            stream.feed_audio(pcm_bytes)
            stream.pump()
            return stream.finish()
        finally:
            self._record_stream_timings(stream)
            stream.close()

    def feed_live_frame(self, frame_bytes):
        """Feed one incoming frame to the live streaming attempt.

        Returns an early-final transcript, or None when only partials
        (or nothing) arrived. Partials update timing state only; they
        are never forwarded. Raises StreamingError on transport
        failure so the handler can abandon the live attempt and keep
        buffering for the normal END_OF_SPEECH path.
        """
        if self._live_stream_failed:
            return None
        if self._live_stream is None:
            self._live_stream = self._new_stream()
            self._streaming_attempted = True
            self._live_stream.connect()
        self._live_stream.feed_audio(frame_bytes)
        events = self._live_stream.pump()
        self.stream_first_partial_ms = self._live_stream.first_partial_ms
        for event in events:
            if event.is_final and event.text and event.text.strip():
                self._record_stream_timings(self._live_stream)
                return event.text
        return None

    def fail_live_stream(self):
        self._live_stream_failed = True
        self.close_live_stream()

    def close_live_stream(self):
        if self._live_stream is not None:
            stream = self._live_stream
            self._live_stream = None
            stream.close()

    def send_early_final(self, text):
        """Reply immediately on an early streaming final (FINAL proto).

        Streaming carries no segment offsets, so both phrase bounds
        are the utterance start, matching an empty-offsets one-shot
        reply. Transcript content is sent to the robot only, never
        logged.
        """
        resp = zmqSTTResponse()
        resp.uuid = self._session_id
        resp.type = resp.ResponseType.FINAL
        resp.timestamp = now_ms()
        resp.speech = text
        start_ts = self._start_ts if self._start_ts else resp.timestamp
        resp.start_timestamp = start_ts
        resp.end_timestamp = start_ts
        self.fell_back = False
        self.stt_provider = STT_PROVIDER_MUSE_STREAMING
        self.stt_source = 'muse_streaming'
        self._parent.zmq_reply(self._device_id, resp)
        logger.info('STT-FINAL: provider=muse_streaming early=True (content not logged)')
        self.close_live_stream()

    def _transcribe_whisper(self, wav_bytes, fell_back):
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

    def _transcribe_oneshot_then_whisper(self, wav_bytes, fell_back):
        try:
            result = transcribe_wav(wav_bytes, get_muse_transcribe_key())
            if result.text and result.text.strip():
                return result.text, result.start_ms, result.end_ms, fell_back, STT_PROVIDER_MUSE_TRANSCRIBE
            logger.warning('Muse Transcribe returned an empty transcript; falling back to Whisper-1')
        except MuseTranscribeError as e:
            # Status/kind only: never the transcript, never the key.
            logger.warning('Muse Transcribe failed (status=%s); falling back to Whisper-1', e.status)
        except Exception as e:
            logger.warning('Muse Transcribe failed (%s); falling back to Whisper-1', type(e).__name__)
        return self._transcribe_whisper(wav_bytes, True)

    def _transcribe(self, wav_bytes):
        """Transcribe one utterance, returning (text, start_ms, end_ms, fell_back, provider).

        Fallback chain, never silent: streaming (only when the
        effective mode is muse_streaming) -> one-shot Muse Transcribe
        -> Whisper-1. Each fallback step is logged with type/status/
        provider only, never transcript content, never a key. A
        Whisper failure still raises, so the caller sends the usual
        error response -- a voice turn is never dropped silently.
        When the effective mode is whisper or muse_oneshot this is
        exactly the pre-streaming behaviour.
        """
        mode, fell_back, _source = self._effective_mode()
        if mode == 'muse_streaming':
            self._streaming_attempted = True
            try:
                text = self._try_streaming(bytes(self._stream_bytes))
                if text and text.strip():
                    return text, 0, 0, False, STT_PROVIDER_MUSE_STREAMING
                logger.warning('Muse streaming returned an empty transcript; falling back to Muse Transcribe one-shot')
            except StreamingError as e:
                logger.warning('Muse streaming failed (kind=%s); falling back to Muse Transcribe one-shot', e.kind)
            except Exception as e:
                logger.warning('Muse streaming failed (%s); falling back to Muse Transcribe one-shot', type(e).__name__)
            return self._transcribe_oneshot_then_whisper(wav_bytes, True)
        if mode == 'muse_oneshot':
            return self._transcribe_oneshot_then_whisper(wav_bytes, fell_back)
        return self._transcribe_whisper(wav_bytes, fell_back)

    def perform(self):
        logger.info(f'Processing session_id {self._session_id} with {len(self._stream_bytes)} bytes')
        self.close_live_stream()
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
            if provider == STT_PROVIDER_MUSE_STREAMING:
                self.stt_source = 'muse_streaming'
            elif provider == STT_PROVIDER_MUSE_TRANSCRIBE:
                self.stt_source = 'muse_transcribe_fallback' if fell_back else 'muse_transcribe'
            else:
                self.stt_source = 'whisper_fallback' if fell_back else 'openai_default'
            if self._streaming_attempted:
                logger.info(f'STT-FINAL: provider={provider} (content not logged)')
            else:
                logger.info(f'STT-FINAL: {text}')
            if fell_back:
                logger.warning(f'STT-FALLBACK: provider={provider} fell_back=True')
        except Exception as e:
            logger.warning(f'Exception handling openAI request: {e}')
            resp.error_code = 66
            resp.error_message = str(e)

        # send response to device
        self._parent.zmq_reply(self._device_id, resp)

        # Streaming turns never write WAVs, even when LOG_WAV is set.
        if LOG_WAV and not self._streaming_attempted:
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
        # Optional injected realtime transport factory (tests / future
        # verified transport). None means the lazy real adapter, which
        # raises StreamingUnavailable offline and falls back -- and
        # disables the per-frame early-final path below entirely.
        self.streaming_websocket_factory = None

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
        sess = self._sessions[sesskey]
        total_sess_bytes = sess.on_request(req)
        # every time we reach EOS, we background it for work
        logger.debug(f'ZMQ Speech VAD: {req.vad} TotalBytes: {total_sess_bytes}')
        # Optional per-frame streaming: only when streaming is the
        # effective mode AND a transport factory is configured. An
        # early final replies now and skips perform(); anything else
        # (partials, failure, no streaming) keeps the existing flow.
        if self.streaming_websocket_factory is not None:
            mode, _fell_back, _source = sess._effective_mode()
            if mode == 'muse_streaming':
                try:
                    early_text = sess.feed_live_frame(req.audio_content)
                except StreamingError as e:
                    logger.warning('Muse streaming live attempt failed (kind=%s); continuing to buffered transcription', e.kind)
                    sess.fail_live_stream()
                    early_text = None
                except Exception as e:
                    logger.warning('Muse streaming live attempt failed (%s); continuing to buffered transcription', type(e).__name__)
                    sess.fail_live_stream()
                    early_text = None
                if early_text:
                    self._sessions.pop(sesskey, None)
                    sess.send_early_final(early_text)
                    return
        if req.vad == req.VADState.END_OF_SPEECH:
            logger.info(f'Session reached END OF SPEECH')
            # session is done, do the work
            sess = self._sessions.pop(sesskey)
            sess.close_live_stream()
            self._worker_queue.submit(sess.perform)
