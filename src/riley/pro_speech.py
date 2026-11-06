"""Riley's voice and hearing from the MacBook Pro's shared speech server (server-management machines/README.md,
"speech"): free and local, no audio leaves our machines.

`POST /tts {text, stack, lang}` streams 16-bit mono PCM at 24 kHz, first sound as soon as it is made. `POST /stt` takes
one utterance as a WAV and returns its text (Parakeet in english-fast); LiveKit cuts utterances with Riley's VAD. \
The VPS reaches
the server through the Pro's SSH tunnel on host port 18799 (com.pukujan.copilot-speech-tunnel), so the worker uses
http://host.docker.internal:18799. Only one stack is resident at a time and the copilot and NYC agents share it:
asking for another stack swaps theirs out once they are idle, and a cold stack takes several seconds to load.
"""
import hashlib
import io
import os
import uuid
import wave
from pathlib import Path

import aiohttp
from livekit import rtc
from livekit.agents import DEFAULT_API_CONNECT_OPTIONS, APIConnectionError, APIConnectOptions, stt, tts
from livekit.agents.language import LanguageCode
from livekit.agents.types import NOT_GIVEN, NotGivenOr
from livekit.agents.utils import AudioBuffer

RATE = 24000
# Lines Riley says the same way on every call, kept as audio. Another app can swap the shared stack out between calls,
# and swapping back took 16.9 s on a real call (5 Nov): the caller heard nothing until then. With the greeting kept
# here, it plays at once and the stack loads behind it. One file per text, voice and stack, so an edit makes a new one.
KEPT = Path(os.environ.get('RILEY_VOICE_DIR', '/tmp/riley-voice'))
# english-fast (Parakeet and Pocket TTS) is English only. The Pro's multilingual stack (Whisper large-v3-turbo and
# Kokoro, voice ef_dora for Spanish; the Pro's agent/languages/es) hears and speaks Spanish; lfm3-audio does too.
# Checked on the Pro, 6 Nov.
SPEAKS_SPANISH = {'multilingual', 'lfm3-audio'}


def stack_for(stack: str, lang: str) -> str:
    """The Pro stack for a call in this language: the configured one, unless it can't do the language."""
    return stack if lang == 'en' or stack in SPEAKS_SPANISH else 'multilingual'


class Switches:
    """Both Pro parts change stack and language together when the caller picks Spanish (riley/main.py _language)."""
    base: str
    stack: str
    lang: str

    def switch(self, lang: str) -> None:
        self.stack, self.lang = stack_for(self.base, lang), lang


class ProSpeechTTS(Switches, tts.TTS):
    def __init__(self, url: str, stack: str = 'english-fast', lang: str = 'en'):
        super().__init__(capabilities=tts.TTSCapabilities(streaming=False), sample_rate=RATE, num_channels=1)
        self.url, self.base, self.stack, self.lang = url.rstrip('/'), stack, stack, lang
        self._http: aiohttp.ClientSession | None = None

    def session(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession()
        return self._http

    async def load(self) -> None:
        """Swap the stack in before the greeting, so the caller does not wait for it (call=1: load now)."""
        async with self.session().post(f'{self.url}/load', params={'stack': self.stack, 'call': '1'},
                                       timeout=aiohttp.ClientTimeout(total=60)) as r:
            r.raise_for_status()

    def synthesize(self, text: str, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS):
        return _Stream(tts=self, input_text=text, conn_options=conn_options)

    async def aclose(self) -> None:
        if self._http and not self._http.closed:
            await self._http.close()

    def _kept(self, text: str) -> Path:
        return KEPT / (hashlib.sha256(f'{self.stack}|{self.lang}|{text}'.encode()).hexdigest()[:16] + '.pcm')

    def kept(self, text: str) -> list[rtc.AudioFrame] | None:
        """The line as audio frames, if a call has said it before."""
        path = self._kept(text)
        if not path.exists():
            return None
        pcm, step = path.read_bytes(), RATE // 10 * 2  # 100 ms frames of 16-bit mono
        return [rtc.AudioFrame(pcm[i:i + step], RATE, 1, len(pcm[i:i + step]) // 2) for i in range(0, len(pcm), step)]

    async def keep(self, text: str) -> None:
        """Synthesize the line once and keep it for the next calls."""
        path = self._kept(text)
        if path.exists():
            return
        pcm = b''.join([bytes(ev.frame.data) async for ev in self.synthesize(text)])
        KEPT.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240 - a small local cache write, once per new line
        path.with_suffix('.part').write_bytes(pcm)
        path.with_suffix('.part').rename(path)


class _Stream(tts.ChunkedStream):
    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        t: ProSpeechTTS = self._tts  # type: ignore[assignment]
        try:
            async with t.session().post(
                    f'{t.url}/tts', json={'text': self.input_text, 'stack': t.stack, 'lang': t.lang},
                    timeout=aiohttp.ClientTimeout(total=60, sock_connect=self._conn_options.timeout)) as r:
                r.raise_for_status()
                output_emitter.initialize(request_id=uuid.uuid4().hex,
                                          sample_rate=int(r.headers.get('X-Sample-Rate', RATE)),
                                          num_channels=1, mime_type='audio/pcm')
                async for chunk in r.content.iter_any():
                    output_emitter.push(chunk)
                output_emitter.flush()
        except aiohttp.ClientError as e:
            raise APIConnectionError(f'pro speech: {e}') from e


class ProSpeechSTT(Switches, stt.STT):
    def __init__(self, url: str, stack: str = 'english-fast', lang: str = 'en'):
        super().__init__(capabilities=stt.STTCapabilities(streaming=False, interim_results=False))
        self.url, self.base, self.stack, self.lang = url.rstrip('/'), stack, stack, lang
        self._http: aiohttp.ClientSession | None = None

    async def _recognize_impl(self, buffer: AudioBuffer, *, language: NotGivenOr[str] = NOT_GIVEN,
                              conn_options: APIConnectOptions) -> stt.SpeechEvent:
        frame = rtc.combine_audio_frames(buffer)
        wav = io.BytesIO()
        with wave.open(wav, 'wb') as w:
            w.setnchannels(frame.num_channels)
            w.setsampwidth(2)
            w.setframerate(frame.sample_rate)
            w.writeframes(bytes(frame.data))
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession()
        lang = language if language else self.lang
        try:
            async with self._http.post(f'{self.url}/stt', params={'stack': self.stack, 'lang': lang},
                                       data=wav.getvalue(),
                                       headers={'Content-Type': 'audio/wav'},
                                       timeout=aiohttp.ClientTimeout(total=20, sock_connect=conn_options.timeout)) as r:
                r.raise_for_status()
                out = await r.json()
        except aiohttp.ClientError as e:
            raise APIConnectionError(f'pro speech: {e}') from e
        return stt.SpeechEvent(type=stt.SpeechEventType.FINAL_TRANSCRIPT, alternatives=[
            stt.SpeechData(language=LanguageCode(out.get('language') or lang), text=out.get('text') or '',
                           confidence=out.get('confidence') or 0.0)])

    async def aclose(self) -> None:
        if self._http and not self._http.closed:
            await self._http.close()
