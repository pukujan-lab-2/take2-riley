"""Riley's voice and hearing from the MacBook Pro's shared speech server (server-management machines/README.md,
"speech"): free and local, no audio leaves our machines.

`POST /tts {text, stack, lang}` streams 16-bit mono PCM at 24 kHz, first sound as soon as it is made. Listening streams:
`GET /stt/stream` is a WebSocket that takes the caller's audio while they talk and returns the turn's text as it ends
(the server's stt_stream.py; Parakeet in english-fast). Without a VAD, `POST /stt` takes one utterance as a WAV.
The VPS reaches the server through the Pro's SSH tunnel on host port 18799 (com.pukujan.copilot-speech-tunnel), so
the worker uses http://host.docker.internal:18799. Only one stack is resident at a time and the copilot and NYC agents
share it: asking for another stack swaps theirs out once they are idle, and a cold stack takes several seconds to load.
"""
import asyncio
import hashlib
import io
import os
import time
import uuid
import wave
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

import aiohttp
from livekit import rtc
from livekit.agents import DEFAULT_API_CONNECT_OPTIONS, APIConnectionError, APIConnectOptions, stt, tts
from livekit.agents.language import LanguageCode
from livekit.agents.types import NOT_GIVEN, NotGivenOr
from livekit.agents.utils import AudioBuffer
from livekit.agents.vad import VAD, VADEventType

RATE = 24000
# Lines Riley says the same way on every call, kept as audio. Another app can swap the shared stack out between calls,
# and swapping back took 16.9 s on a real call (5 Nov): the caller heard nothing until then. With the greeting kept
# here, it plays at once and the stack loads behind it. One file per text, voice and stack, so an edit makes a new one.
KEPT = Path(os.environ.get('RILEY_VOICE_DIR', '/tmp/riley-voice'))
# english-fast (Parakeet and Pocket TTS) is English only. The Pro's multilingual stack (Whisper large-v3-turbo and
# Kokoro, voice ef_dora for Spanish; the Pro's agent/languages/es) hears and speaks Spanish; lfm3-audio does too.
# Checked on the Pro, 6 Nov.
SPEAKS_SPANISH = {'multilingual', 'lfm3-audio'}
# A call the speech gateway had no room for (riley/speech_route.py): the Pro parts stand aside for the next entry in
# the chain, if screen.yaml lists one
NO_ROOM = 'pro speech: no speech server has room for this call'


def stack_for(stack: str, lang: str) -> str:
    """The Pro stack for a call in this language: the configured one, unless it can't do the language."""
    return stack if lang == 'en' or stack in SPEAKS_SPANISH else 'multilingual'


class Switches:
    """Both Pro parts change stack and language together when the caller picks Spanish (riley/main.py _language)."""
    base: str
    stack: str
    lang: str
    headers: Mapping[str, str] = MappingProxyType({})  # the speech gateway's call and project (riley/speech_route.py)

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
                                       headers=self.headers, timeout=aiohttp.ClientTimeout(total=60)) as r:
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
        if not t.url:
            raise APIConnectionError(NO_ROOM)
        try:
            async with t.session().post(
                    f'{t.url}/tts', json={'text': self.input_text, 'stack': t.stack, 'lang': t.lang}, headers=t.headers,
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
    """With Riley's VAD it streams (_Listening); without one each utterance is a POST /stt."""

    def __init__(self, url: str, stack: str = 'english-fast', lang: str = 'en', vad: VAD | None = None):
        super().__init__(capabilities=stt.STTCapabilities(streaming=vad is not None, interim_results=False))
        self.url, self.base, self.stack, self.lang, self.vad = url.rstrip('/'), stack, stack, lang, vad
        self._http: aiohttp.ClientSession | None = None

    def session(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession()
        return self._http

    def stream(self, *, language: NotGivenOr[str] = NOT_GIVEN,
               conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS) -> stt.RecognizeStream:
        return _Listening(stt=self, conn_options=conn_options)

    async def _recognize_impl(self, buffer: AudioBuffer, *, language: NotGivenOr[str] = NOT_GIVEN,
                              conn_options: APIConnectOptions) -> stt.SpeechEvent:
        frame = rtc.combine_audio_frames(buffer)
        wav = io.BytesIO()
        with wave.open(wav, 'wb') as w:
            w.setnchannels(frame.num_channels)
            w.setsampwidth(2)
            w.setframerate(frame.sample_rate)
            w.writeframes(bytes(frame.data))
        lang = language if language else self.lang
        if not self.url:
            raise APIConnectionError(NO_ROOM)
        try:
            async with self.session().post(f'{self.url}/stt', params={'stack': self.stack, 'lang': lang},
                                       data=wav.getvalue(),
                                       headers={'Content-Type': 'audio/wav', **self.headers},
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


# This long a pause after speech and the turn's text so far is made, so it is ready if the turn ends there (0.3 s of
# quiet, the VAD's min_silence_duration). A shorter pause makes more transcripts that are thrown away when talk resumes.
PAUSE_S = 0.1
SR = 16000


class _Listening(stt.RecognizeStream):
    """The caller's speech goes to the Pro while they talk, cut into turns by Riley's VAD; the text comes back as the
    turn ends, usually already made during the pause before it."""

    def __init__(self, *, stt: ProSpeechSTT, conn_options: APIConnectOptions):
        super().__init__(stt=stt, conn_options=conn_options, sample_rate=SR)

    async def _run(self) -> None:
        t: ProSpeechSTT = self._stt  # type: ignore[assignment]
        assert t.vad is not None
        if not t.url:
            raise APIConnectionError(NO_ROOM)
        vad = t.vad.stream()
        sock: dict = {}  # the open socket: ws, the stack it was opened for, its replies and their reader
        turn: list = []  # this turn's frames, sent again if the socket has to be opened again mid-turn
        sent, paused = 0, False  # samples sent this turn; a transcript was asked for at this pause

        async def connect() -> None:
            await close()
            try:
                ws = await t.session().ws_connect(f"{t.url.replace('http', 'ws', 1)}/stt/stream", heartbeat=20,
                                                  headers=t.headers,
                                                  params={'stack': t.stack, 'lang': t.lang, 'sr': str(SR)})
            except aiohttp.ClientError as e:
                raise APIConnectionError(f'pro speech: {e}') from e
            replies: asyncio.Queue = asyncio.Queue()
            sock.update(ws=ws, stack=t.stack, replies=replies, reading=asyncio.create_task(receive(ws, replies)))

        async def close() -> None:
            if sock:
                sock['reading'].cancel()
                await sock['ws'].close()
                sock.clear()

        async def receive(ws, replies):
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    replies.put_nowait(msg.json())
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
            replies.put_nowait(None)

        async def send(frames=None, message=None) -> bool:
            """False when the socket has closed under us; the turn is sent again at its end."""
            try:
                for f in frames or ():
                    await sock['ws'].send_bytes(bytes(f.data))
                if message:
                    await sock['ws'].send_json(message)
                return True
            except (ConnectionResetError, aiohttp.ClientError):
                return False

        async def final(message) -> dict | None:
            if await send(message=message):
                while (out := await sock['replies'].get()) is not None and out.get('type') != 'final':
                    pass
                if out is not None:
                    return out
            return None

        async def forward():
            async for f in self._input_ch:
                if isinstance(f, self._FlushSentinel):
                    vad.flush()
                else:
                    vad.push_frame(f)
            vad.end_input()

        async def audio(frames):
            nonlocal sent
            turn.extend(frames)
            sent += sum(f.samples_per_channel for f in frames)
            await send(frames)

        async def turns():
            nonlocal sent, paused
            async for ev in vad:
                if ev.type == VADEventType.START_OF_SPEECH:
                    sent, paused = 0, False
                    turn.clear()
                    # The caller picked another language since the socket opened: the gateway places the call on
                    # the new stack's server, so open the socket again rather than have it closed mid-turn
                    if not sock or sock['ws'].closed or sock['stack'] != t.stack:
                        await connect()
                    self._event_ch.send_nowait(stt.SpeechEvent(type=stt.SpeechEventType.START_OF_SPEECH))
                    await audio(ev.frames)  # the speech so far, with the VAD's padding before it
                elif ev.type == VADEventType.INFERENCE_DONE and ev.speaking:
                    await audio(ev.frames)
                    if ev.raw_accumulated_silence >= PAUSE_S and not paused:
                        paused = True
                        await send(message={'type': 'decode', 'stack': t.stack, 'lang': t.lang})
                    elif ev.raw_accumulated_silence == 0:
                        paused = False
                elif ev.type == VADEventType.END_OF_SPEECH:
                    self._event_ch.send_nowait(stt.SpeechEvent(
                        type=stt.SpeechEventType.END_OF_SPEECH,
                        speech_end_time=time.time() - ev.silence_duration - ev.inference_duration))
                    end = {'type': 'end', 'speech_end': sent - int(ev.silence_duration * SR), 'stack': t.stack,
                           'lang': t.lang}
                    out = await final(end)
                    if out is None:  # closed mid-turn (the call moved, the gateway restarted): once more, whole turn
                        await connect()
                        out = await final(end) if await send(turn) else None
                    if out is None:
                        raise APIConnectionError('pro speech: the listening socket closed')
                    self._mark_provider_progress()
                    if out.get('text'):
                        self._event_ch.send_nowait(stt.SpeechEvent(
                            type=stt.SpeechEventType.FINAL_TRANSCRIPT, alternatives=[
                            stt.SpeechData(language=LanguageCode(out.get('language') or t.lang), text=out['text'],
                                           confidence=out.get('confidence') or 0.0)]))

        await connect()
        try:
            await asyncio.gather(forward(), turns())
        finally:
            await vad.aclose()
            await close()
