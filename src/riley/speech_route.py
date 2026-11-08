"""Riley's speech through the shared speech gateway (the owner's call, 7 Nov: self-hosted first, to save money; the
gateway is voice-agent-research docs/speech-gateway.md and decision 0023).

One gateway on the VPS fronts every project's speech servers and its routes.toml decides where a call goes: English to
the Pro (Parakeet and Pocket TTS), every other language to the Windows PC (Whisper large-v3-turbo and Kokoro on its
RTX 4060), which also takes English when the Pro is full. Riley sends its room on every speech request (X-Speech-
Call), so a call stays on one server, and asks for a place when the call starts and again when the caller picks a
language (riley/main.py _language). With no room anywhere (503) the Pro parts stand down and the next voice in the
chain, the cloud, takes the call. The call keeps its place every KEEP_S and gives it back when it ends.
"""
import asyncio
import contextlib
import os
from dataclasses import dataclass

import aiohttp

from .pro_speech import ProSpeechSTT, ProSpeechTTS, stack_for

KEEP_S = 30  # the gateway frees a call it hasn't heard from in 90 s


@dataclass(frozen=True)
class Gateway:
    url: str
    project: str

    def headers(self, call: str | None = None) -> dict[str, str]:
        return {'X-Speech-Project': self.project, **({'X-Speech-Call': call} if call else {})}


def gateway(entry: dict | None) -> Gateway | None:
    """screen.yaml stack.speech_gateway: {url_env, url, project}."""
    if not entry:
        return None
    url = (os.environ.get(entry['url_env'], '') if entry.get('url_env') else '') or entry['url']
    return Gateway(url.rstrip('/'), entry.get('project', 'riley'))


class Route:
    """One call's place behind the gateway, and the Pro parts (listening and voice) pointed at it."""

    def __init__(self, room: str, gw: Gateway, parts: list):
        self.room, self.gw, self.parts = room, gw, parts
        self.host: str | None = None  # the speech server the gateway placed the call on
        self._http: aiohttp.ClientSession | None = None
        self._keeper: asyncio.Task | None = None
        self.loading: asyncio.Future | None = None

    def session(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2))
        return self._http

    def stack(self, lang: str) -> str:
        return stack_for(next((p.base for p in self.parts), 'english-fast'), lang)

    async def _place(self, lang: str) -> str | None:
        try:
            async with self.session().post(f'{self.gw.url}/place', params={'stack': self.stack(lang)},
                                           headers=self.gw.headers(self.room)) as r:
                return (await r.json()).get('host') if r.status == 200 else None
        except (aiohttp.ClientError, TimeoutError, ValueError):
            return None  # no gateway: the cloud takes the call

    async def to(self, lang: str) -> str | None:
        """Take a place for the call in this language and point the parts at the gateway, or stand them down."""
        self.host = await self._place(lang)
        for p in self.parts:
            p.url = self.gw.url if self.host else ''  # no room: the parts fail at once and the cloud takes over
            p.headers = self.gw.headers(self.room)
            p.switch(lang)
        print('  speech       ', self.host or 'cloud', lang, flush=True)
        if self.host:
            # The load runs behind the kept greeting (or Riley's thinking, after a language pick); a cold stack takes
            # 4.5 to 17 s. Awaiting self.loading waits for it
            self.loading = asyncio.ensure_future(self._load(lang))
            if self._keeper is None:
                self._keeper = asyncio.ensure_future(self._keep())
        return self.host

    async def _load(self, lang: str) -> None:
        # call=1: a person is waiting, so it loads even when a keep-warm load would be held off
        with contextlib.suppress(aiohttp.ClientError, TimeoutError):
            async with self.session().post(f'{self.gw.url}/load', params={'stack': self.stack(lang), 'call': '1'},
                                           headers=self.gw.headers(self.room),
                                           timeout=aiohttp.ClientTimeout(total=60)) as r:
                r.raise_for_status()

    async def _keep(self) -> None:
        while True:
            await asyncio.sleep(KEEP_S)
            if not self.host:
                continue
            with contextlib.suppress(aiohttp.ClientError, TimeoutError, ValueError):
                async with self.session().post(f'{self.gw.url}/lease', params={'id': self.room},
                                               headers=self.gw.headers(self.room)) as r:
                    if not (await r.json()).get('ok'):  # it ran out (the gateway restarted): take it again
                        self.host = await self._place(next((p.lang for p in self.parts), 'en'))

    async def close(self) -> None:
        for task in (self._keeper, self.loading):
            if task:
                task.cancel()
        if self.host:
            with contextlib.suppress(aiohttp.ClientError, TimeoutError):
                async with self.session().post(f'{self.gw.url}/release', headers=self.gw.headers(self.room)):
                    pass
        if self._http and not self._http.closed:
            await self._http.close()


async def warm(gw: Gateway) -> None:
    """Load Riley's stacks (routes.toml [project.riley] warm) ahead of a call: a call starting, the site opened. The
    gateway answers at once and loads behind it, never swapping out a stack another call is using."""
    with contextlib.suppress(aiohttp.ClientError, TimeoutError):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as http, \
                http.post(f'{gw.url}/warm', headers=gw.headers()):
            pass


def pro_parts(parts: list) -> list:
    return [p for p in parts if isinstance(p, (ProSpeechSTT, ProSpeechTTS))]
