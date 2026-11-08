"""Riley's speech chains as screen.yaml has them since 8 Nov (the owner's call: no cloud speech by default): one entry
each, our own speech through the gateway, and Dana's kept clips playing with no voice set to record new ones."""
from aiohttp import web
from livekit.agents import stt, tts

from riley import main, outcome, pro_speech, speech_route
from riley.pro_speech import ProSpeechSTT, ProSpeechTTS


class _Vad:  # stands in for Silero: an ear with a VAD streams
    pass


def test_a_one_entry_chain_is_used_as_it_is():
    stack = main.config()['stack']
    ears = [main.ear(e, _Vad()) for e in stack['listens']]
    chain = main.voices(stack['speaks'])
    assert len(ears) == len(chain) == 1
    assert isinstance(main.hearing(ears, _Vad()), ProSpeechSTT)
    assert isinstance(main.speaking(chain), ProSpeechTTS)


def test_two_entries_still_make_a_fallback_chain():
    ears = [ProSpeechSTT('http://a', vad=_Vad()), ProSpeechSTT('http://b', vad=_Vad())]
    voices = [ProSpeechTTS('http://a'), ProSpeechTTS('http://b')]
    assert isinstance(main.hearing(ears, _Vad()), stt.FallbackAdapter)
    assert isinstance(main.speaking(voices), tts.FallbackAdapter)


def test_with_no_dana_voice_the_kept_clips_still_play_and_nothing_is_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr(pro_speech, 'KEPT', tmp_path)
    monkeypatch.setenv('CARTESIA_API_KEY', 'set')  # a key alone never records: the voice must be in screen.yaml
    cfg = outcome.config()
    assert 'voice' not in cfg['dana']
    assert outcome.dana_voice(cfg) is None
    kinds = list(outcome.dana_lines(cfg))
    kept = outcome._clip(outcome.dana_lines(cfg)[kinds[0]])
    kept.write_bytes(b'RIFF')
    assert outcome.clip_path(kinds[0], cfg) == kept
    assert outcome.clip_path(kinds[1], cfg) is None  # a line with no kept clip goes without one


async def test_with_no_dana_voice_keep_clips_records_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(pro_speech, 'KEPT', tmp_path)
    cfg = outcome.config()
    first = next(iter(outcome.dana_lines(cfg).values()))
    outcome._clip(first).write_bytes(b'RIFF')
    assert await outcome.keep_clips(cfg) == [next(iter(outcome.dana_lines(cfg)))]
    assert [p.name for p in tmp_path.iterdir()] == [outcome._clip(first).name]


async def test_warm_names_the_project_to_the_gateway():
    got: list = []

    async def warm(request):
        got.append((request.query.get('project'), request.headers.get('X-Speech-Project')))
        return web.json_response({'ok': True})
    app = web.Application()
    app.router.add_post('/warm', warm)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    url = f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}'
    await speech_route.warm(speech_route.Gateway(url, 'riley'))
    await runner.cleanup()
    assert got == [('riley', 'riley')]
