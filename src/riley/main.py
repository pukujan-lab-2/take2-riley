"""Riley's calls: src/agent.py (AGENT_PROFILE=riley) hands each call to the number (or a browser test room) here.

  caller ──phone──► LiveKit number ──dispatch rule──► room ◄──► this worker
    our own speech servers listen and speak (screen.yaml stack.listens and stack.speaks, through the speech gateway),
    the brain chain (screen.yaml stack.brain) decides;
    the turn detector and Silero VAD end turns and handle barge-in; screen.yaml is re-read for every call.

Each call is stored in Octo (riley/octo.py): the transcript, the recorded answers, the scorecard and the latency of \
every
turn go to the workspace's Postgres, and the spans of every turn to its Phoenix. calls/<room>.json keeps a local copy.

Run:  AGENT_PROFILE=riley AGENT_NAME=nurse-screen uv run src/agent.py dev   (call the number or use the Agents Playground)
      AGENT_PROFILE=riley uv run src/agent.py console                      (this Mac's mic and speaker)
The starter's pieces are used here as in src/agent.py: the LiveKit Inference turn detector, preemptive generation and
ai_coustics noise cancellation. Riley keeps its own silence check-in (_quiet below), which the hard-caller evals replay.
"""
import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import yaml
from livekit.agents import AgentSession, JobContext, JobProcess, inference, room_io, stt, tts
from livekit.agents.beta.tools import EndCallTool
from livekit.plugins import ai_coustics, cartesia, deepgram, silero

from . import (
    brain,
    followup,
    guest,
    guest_call,
    helpline,
    keys,
    match,
    octo,
    outbound,
    outcome,
    outcomeline,
    scheduler,
    speech_route,
    store,
    tenant,
)
from .pro_speech import ProSpeechSTT, ProSpeechTTS
from .screener import END_WORDS, Screen, Screener, carry_over, greeting, job_config, settled

keys.load()
ROOT = Path(__file__).resolve().parents[2]
CALLS = ROOT / 'calls'
# Words Deepgram should expect on a nursing screen.
KEYTERMS = ['RN', 'BLS', 'ACLS', 'PALS', 'med-surg', 'telemetry', 'step-down', 'ICU', 'per diem', 'travel nurse']
# What Riley does when the caller goes quiet: check in once, then wrap up. Kept here so evals/hard_callers.py replays
# the same lines.
CHECK_IN = ('The caller has gone quiet. Gently check they are still there, then repeat your last question in a few '
            'words.')


def give_up(recruiter: str) -> str:
    return (f"It sounds like now isn't a good time. No problem, {recruiter} will reach out to pick this up. "
            f"Thanks, and take care.")

def config() -> dict:
    return yaml.safe_load((ROOT / 'screen.yaml').read_text())


def prewarm(proc: JobProcess):
    proc.userdata['vad'] = silero.VAD.load(min_silence_duration=0.3)


def ear(listens: dict, vad):
    """One entry of screen.yaml stack.listens: the first hears, the next takes over when it fails. The Pro listens
    while the caller talks, its turns cut by Riley's VAD."""
    if listens['provider'] == 'pro-speech':
        return ProSpeechSTT(os.environ.get('PRO_SPEECH_URL', 'http://host.docker.internal:18799'),
                            stack=listens.get('stack', 'english-fast'), vad=vad)
    # DEEPGRAM_BASE_URL routes through the Octo vault proxy with a working key
    proxy: dict[str, Any] = {'base_url': os.environ['DEEPGRAM_BASE_URL']} if os.environ.get('DEEPGRAM_BASE_URL') else {}
    return deepgram.STT(model=listens['model'], keyterm=KEYTERMS, smart_format=True, **proxy)


def hearing(ears: list, vad):
    """The chain of ears as one: a single streaming ear on its own (screen.yaml has one entry since 8 Nov), else a
    fallback chain."""
    return ears[0] if len(ears) == 1 and ears[0].capabilities.streaming else stt.FallbackAdapter(ears, vad=vad)


def speaking(chain: list):
    """The chain of voices as one: a single voice on its own, else a fallback chain."""
    return chain[0] if len(chain) == 1 else tts.FallbackAdapter(chain)


def voices(speaks: list[dict]) -> list:
    """Riley's voice chain, from screen.yaml stack.speaks: the first speaks, the next takes over when it fails."""
    return [voice(v) for v in speaks]


def voice(speaks: dict):
    if speaks['provider'] == 'pro-speech':
        return ProSpeechTTS(os.environ.get('PRO_SPEECH_URL', 'http://host.docker.internal:18799'),
                            stack=speaks.get('stack', 'english-fast'))
    # CARTESIA_BASE_URL routes through the Octo vault proxy with a working key
    proxy: dict[str, Any] = {'base_url': os.environ['CARTESIA_BASE_URL']} if os.environ.get('CARTESIA_BASE_URL') else {}
    return cartesia.TTS(model=speaks['model'], speed=speaks.get('speed'), **proxy)


def latency(history: list[dict]) -> list[dict]:
    """Per reply: how long the caller waited from the end of their speech to the agent's first audio, and its parts."""
    rows, eot, stopped = [], None, None
    for item in history:
        m = item.get('metrics') or {}
        if item.get('role') == 'user':
            eot, stopped = m.get('end_of_turn_delay'), m.get('stopped_speaking_at')
        elif item.get('role') == 'assistant' and 'llm_node_ttft' in m:
            parts = {'end_of_turn': eot, 'brain_first_token': m.get('llm_node_ttft'),
                     'voice_first_audio': m.get('tts_node_ttfb')}
            row = {**{k: round(v, 3) for k, v in parts.items() if v is not None},
                   'total': round(sum(v for v in parts.values() if v), 3),
                   'model': (m.get('llm_metadata') or {}).get('model_name')}
            # The parts miss a model call that only ran a tool before the one that spoke, and transcription; the
            # clock from the caller's last word to Riley's first sound does not (caller bot, 4 Nov)
            if stopped and m.get('started_speaking_at'):
                row['heard'] = round(m['started_speaking_at'] - stopped, 3)
            rows.append(row)
            eot, stopped = None, None
    return rows


def picked_job(meta: dict, jobs: list[dict]) -> dict | None:
    """The open job a browser call's dispatch names ({'job_id'} from /try), or None: an id not open is ignored."""
    try:
        job_id = int(meta.get('job_id') or '')
    except (TypeError, ValueError):
        return None
    return next((j for j in jobs if j['id'] == job_id), None)


async def entrypoint(ctx: JobContext):
    cfg = config()
    # A call Riley placed (riley/outbound.py) carries {'mode', 'candidate_id', 'application_id'} in its dispatch
    try:
        meta = json.loads(ctx.job.metadata or '{}')
    except ValueError:
        meta = {}
    stack = cfg['stack']
    records = cfg.get('records', {})
    traces = octo.tracer(ctx.room.name, content=records.get('traces_content', True)) if octo.ready() else None
    # Rooms the caller bot opens start with bot-, browser calls from the story page with web-; the rest came in on the
    # phone
    source = 'bot' if ctx.room.name.startswith('bot-') else 'web' if ctx.room.name.startswith('web-') else 'phone'
    llm_, chain = brain.build(stack)
    loading: set[asyncio.Future] = set()  # kept so a background load is not garbage-collected mid-swap
    ears: list[stt.STT] = [ear(e, ctx.proc.userdata['vad']) for e in stack['listens']]
    stt_ = hearing(ears, ctx.proc.userdata['vad'])
    chain_v = voices(stack['speaks'])
    tts_ = speaking(chain_v)
    pro = next((v for v in chain_v if isinstance(v, ProSpeechTTS)), None)
    # Our own speech servers first, through the shared speech gateway: the call takes a place on the one for its
    # language that has room (riley/speech_route.py)
    speech_gw = speech_route.gateway(stack.get('speech_gateway'))
    route = speech_route.Route(ctx.room.name, speech_gw, speech_route.pro_parts([*ears, *chain_v])) \
        if speech_gw and pro else None
    if route:
        # A browser call can name its language; a phone call asks (set_language)
        await route.to(meta.get('lang') or 'en')
        ctx.add_shutdown_callback(route.close)
        # Riley's other stacks load too, so a caller who picks another language doesn't wait for one
        loading.add(task := asyncio.ensure_future(speech_route.warm(route.gw)))
        task.add_done_callback(loading.discard)
    greeting_audio = pro.kept(greeting(cfg)) if pro and pro.url else None

    async def _warm(pro: ProSpeechTTS):
        # A cold stack takes up to 17 s to load. It loads while the kept greeting plays (or, on a worker's first call,
        # before the greeting is synthesized), then the greeting is kept for the next call
        try:
            if route:
                if route.loading:
                    await route.loading
            else:
                await pro.load()
            if pro.url:
                await pro.keep(greeting(cfg))
        except Exception as e:  # the first line then loads it, or the next voice takes over
            print('pro speech load failed:', type(e).__name__, e, flush=True)
    warming = asyncio.ensure_future(_warm(pro)) if pro else None
    if warming and not greeting_audio:
        await warming
    # A known number: Riley confirms what the resume says and skips what they answered for another role (SPEC §3)
    # A known number with nothing left to screen gets the help line instead: their status, never a score change
    app, prefilled, carried, rows, number, demo = None, {}, {}, [], '', None
    # The scheduler's calls (riley/scheduler.py): a callback is their own call placed by Riley, so it finds the
    # record by number the same way; a follow-up gives the updates named in event_ids
    callback, follow, follow_who = meta.get('mode') == 'callback', None, None
    # Which employer the call is for (riley/tenant.py): the record a placed call names, else the number dialled.
    # Hudson Park's (`live`) when neither says otherwise, as before
    home = tenant.LIVE
    if source == 'web' and octo.ready():
        try:
            home = await tenant.home(meta)
        except Exception as e:
            print('org lookup failed:', type(e).__name__, e, flush=True)
    if source == 'phone' and octo.ready():
        try:
            await ctx.connect()
            await store.ensure()
            # A guest from /try (track 9): the call Riley placed names the application; their own call finds it by
            # number. Either way the made-up name and role stand in for the identity check
            if meta.get('mode') == 'guest' and meta.get('application_id'):
                demo = await guest.by_application(int(meta['application_id']))
            caller = await asyncio.wait_for(ctx.wait_for_participant(), 3 if not meta else 30)
            number = caller.attributes.get('sip.phoneNumber', '')
            home = await tenant.home(meta, caller.attributes.get('sip.trunkPhoneNumber'))
            said = await tenant.names_for(home, cfg)
            if said['org']:  # Riley says this employer's name, never Hudson Park's
                cfg = tenant.branded(cfg, said)
                greeting_audio = None
            for _ in range(60):  # a call Riley placed: the person is in the room while it rings; speak once answered
                if not meta or caller.attributes.get('sip.callStatus', 'active') == 'active':
                    break
                await asyncio.sleep(0.5)
            # The demo's order on one number: screen, help line until the outcome rings, the outcome call, then
            # their next own call starts the interview again
            if number and not meta:  # a /try guest's records are in guest.SANDBOX, everyone else's in their org's
                for where in (guest.SANDBOX, home):
                    if outcome.auto_decides(await outcome.get_mode(where)) and \
                            await store.restart_after_outcome(where, number):
                        break
            demo = demo or (await guest.find(number) if number and (not meta or callback) else None)
            if demo:  # screened for the job they were given, with its facts and questions
                cfg = job_config(cfg, demo)
                app = {'id': demo['application_id'], 'candidate_id': demo['candidate_id']}
                prefilled = guest.resume_answers(demo)  # their made-up resume, read back and one gap asked
            else:
                app, prefilled, carried = await store.for_call(home, number, meta.get('application_id'))
            rows = [] if app or (meta and not callback) else await store.status(home, number)
            extras = await scheduler.follow_up_items(meta.get('event_ids') or [])
            if meta.get('mode') == 'follow_up' and extras and meta.get('application_id'):
                oa = await outcome.application(int(meta['application_id']))
                follow = followup.FollowUp(cfg, extras, scheduler.lines(extras, cfg['agent']['recruiter'],
                                                                        oa.get('timezone') or scheduler.TZ))
                follow_who = followup.who_for(oa)
                follow.items = [{**i, 'sandbox': oa['sandbox']} for i in extras]
        except Exception as e:  # an unknown or slow lookup is a plain screen, never a failed call
            print('application lookup failed:', type(e).__name__, e, flush=True)
    # Track 6: an approved outcome waiting for this caller, or an outbound call placed for one, is given first
    # (riley/outcomeline.py). Dana's recorded lines are made in the background if this worker has none yet.
    out: outcomeline.Outcome | None = None
    if source == 'phone' and octo.ready() and meta.get('mode') not in ('guest', 'status', 'follow_up', 'screen'):
        try:
            found = await outcomeline.lookup(home, number, ctx.room.metadata)
            if found:
                m, oa = found
                if tenant.is_org(m.get('sandbox')):  # the outcome is said in the name of the person who decided
                    p = outcomeline.parts(m)
                    cfg = tenant.branded(cfg, {**await tenant.names_for(m['sandbox'], cfg),
                                               **({'recruiter': p['recruiter']} if p.get('recruiter') else {})})
                # Other updates due the same day ride along on the outcome call (riley/scheduler.py)
                said = scheduler.lines(await scheduler.follow_up_items(meta.get('event_ids') or []),
                                       cfg['agent']['recruiter'], oa.get('timezone') or scheduler.TZ)
                out = outcomeline.Outcome(cfg, m, oa, language=oa.get('language') or None, extra=said)
                if not outcome.clip_path(m['kind'], cfg):
                    outcome.in_background(outcome.keep_clips(cfg))
        except Exception as e:  # no outcome found is the usual call, never a failed one
            print('outcome lookup failed:', type(e).__name__, e, flush=True)
    # A caller not on file picks one of the open jobs on the call; a known one is screened for the job they applied to
    jobs: list[dict] = []
    if app and app.get('questions'):
        cfg = job_config(cfg, {**app, 'facts': app.get('job_facts')})
    carried = carry_over(cfg, carried)
    if not app and not rows and octo.ready():
        try:
            jobs = await asyncio.wait_for(match.open_jobs(home), 3)
        except Exception as e:  # no list: the screen for screen.yaml's job, as before
            print('open jobs lookup failed:', type(e).__name__, e, flush=True)
    # A browser call from /try with a role picked (web/server.py api_talk): that job's questions and facts, no asking
    picked = picked_job(meta, jobs) if source == 'web' else None
    if picked:
        cfg, jobs = job_config(cfg, picked), []
    screen = Screen(cfg=cfg, prefilled=prefilled, answers=settled(carried), job=picked)
    help = helpline.Help(cfg, rows, rows[0].get('timezone') or 'America/New_York') if rows and not out else None
    state = follow or out or help or screen  # what the silence check-in and the speech filter read
    # A number on file: Riley checks the name and role before it says anything about them (riley/identity.py)
    on_file = None if demo or follow else (out.application if out else None) or app or (rows[-1] if rows else None)
    who = ({'name': on_file.get('name') or cfg['agent'].get('unknown_caller_name', 'John Doe'),
            'title': on_file['title'], 'applied': store.ts(on_file.get('applied'))} if on_file else None)
    who = follow_who if follow else who
    if who:
        state.identity = 'pending'
    session = AgentSession(
        userdata=state,
        vad=ctx.proc.userdata['vad'],
        stt=stt_,
        llm=llm_,
        tts=tts_,
        turn_handling={
            # The starter's turn detector (LiveKit Inference): words and tone together
            'turn_detection': inference.TurnDetector(),
            # The turn detector waits longer when a sentence sounds unfinished ("I've been a nurse for, um...").
            # Capped at 1.5 s: at 3.0 s a short final answer like "I worked as a PCT" sat out the whole cap
            # (test call, 4 Nov).
            'endpointing': {'mode': 'dynamic', 'min_delay': 0.4, 'max_delay': 1.5},
            # Half a second of real speech stops Riley; a cough or "mm-hm" that turns out not to be a turn lets it
            # resume.
            'interruption': {'min_duration': 0.5, 'resume_false_interruption': True, 'false_interruption_timeout': 1.5},
            'preemptive_generation': {'enabled': True},
        },
        user_away_timeout=cfg['rules']['silence_seconds'],
    )
    print('call', ctx.room.name, 'brain chain', ' > '.join(chain), 'ears',
          ' > '.join(v['provider'] for v in stack['listens']), 'voice',
          ' > '.join(v['provider'] for v in stack['speaks']), flush=True)

    if out:
        @session.on('user_input_transcribed')
        def _greeting(ev):
            # A result call that reaches a recorded greeting is rung again later (outcomeline.save)
            if ev.is_final and outcomeline.VOICEMAIL.search(ev.transcript or ''):
                out.voicemail = True

    @session.on('user_state_changed')
    def _quiet(ev):
        # Only silences in a row count: a caller who paused once early and once much later was hung up on at the
        # second pause, mid-screen (difficult caller "silent", 6 Nov)
        if ev.new_state == 'speaking':
            state.went_quiet = 0
            return
        if ev.new_state != 'away' or session.agent_state == 'speaking':
            return
        state.went_quiet += 1
        if state.went_quiet == 1:
            state.checking_in = True
            session.generate_reply(instructions=CHECK_IN)
        else:
            state.ended_by = 'silence'
            handle = session.say(give_up(cfg['agent']['recruiter']), allow_interruptions=False)
            handle.add_done_callback(lambda _: session.shutdown())

    async def _save_help():
        assert help is not None  # only registered for help calls
        try:
            await helpline.save(home, rows[0]['candidate_id'], help)
            print('help call stored', ctx.room.name, len(help.requests), 'requests', flush=True)
        except Exception as e:
            print('help call store failed', ctx.room.name, e, flush=True)
        if traces:
            traces.force_flush()

    async def _save_outcome():
        assert out is not None  # only registered for outcome calls
        try:
            await outcomeline.save(out.message['sandbox'], out)
            print('outcome call stored', ctx.room.name, out.identity, out.booked, flush=True)
        except Exception as e:
            print('outcome call store failed', ctx.room.name, e, flush=True)
        if traces:
            traces.force_flush()

    async def _save_follow_up():
        assert follow is not None  # only registered for follow-up calls
        try:
            await followup.save(follow.items[0].get('sandbox') or home, follow)
            print('follow-up call stored', ctx.room.name, follow.identity, follow.reconfirmed, flush=True)
        except Exception as e:
            print('follow-up call store failed', ctx.room.name, e, flush=True)
        if traces:
            traces.force_flush()

    async def _callback(candidate_id: int, application_id: int | None):
        # They asked Riley to call back at a time they chose: scheduled work the scheduler rings for
        if not screen.callback:
            return
        try:
            due = await scheduler.queue_callback(guest.SANDBOX if demo else home, candidate_id, application_id,
                                                 screen.callback)
            print('callback queued', application_id, due.isoformat(), flush=True)
        except Exception as e:
            print('callback queue failed', application_id, type(e).__name__, e, flush=True)

    async def _save():
        history = session.history.to_dict(exclude_timestamp=False).get('items', [])
        CALLS.mkdir(exist_ok=True)
        record = {
            'room': ctx.room.name, 'started': screen.started, 'seconds': round(time.time() - screen.started),
            'brain_chain': chain, 'ended_by': screen.ended_by or 'call ended',
            'scorecard': screen.scorecard(), 'answers': screen.answers, 'caller_questions': screen.caller_questions,
            'recruiter_requested': screen.recruiter_requested, 'callback': screen.callback,
            'language': screen.language,
            'latency': latency(history), 'transcript': [
                {'role': i['role'], 'text': ' '.join(c for c in i.get('content', []) if isinstance(c, str)),
                 'interrupted': i.get('interrupted', False),
                 'at': round(i['created_at'] - screen.started, 1) if i.get('created_at') else None}
                for i in history if i.get('type') == 'message' and i.get('role') in ('user', 'assistant')],
        }
        (CALLS / f'{ctx.room.name}.json').write_text(json.dumps(record, indent=2))
        print('saved', CALLS / f'{ctx.room.name}.json', record['scorecard'], flush=True)
        if octo.ready():
            # Bot calls are synthetic, so their transcripts are public; phone calls follow screen.yaml records:
            public = source == 'bot' or records.get('publish_phone_transcripts', False)
            try:
                # a /try guest's call is the demo's, never in Hudson Park's call list; an org's is in its own
                await octo.save_call(record, source, public, sandbox=guest.SANDBOX if demo else home)
                print('stored in octo', ctx.room.name, flush=True)
            except Exception as e:  # the local copy above still has it
                print('octo store failed', ctx.room.name, e, flush=True)
            try:
                if screen.callback and screen.identity != 'not_them' and not screen.answers and not app and number:
                    # A new caller who had no time at all: kept as a person, so the callback has someone to ring
                    who_new = await store.upsert_candidate(home, phone=number)
                    await _callback(who_new['id'], None)
                if screen.identity == 'not_them' or not screen.answers:
                    if app and screen.identity != 'not_them':
                        await _callback(app['candidate_id'], app['id'])
                    # someone else on their phone, or nothing said: the record is left as it was
                elif app:
                    await store.save_answers(app['id'], screen.answers, record['scorecard'], screen.prefilled)
                    await _contact(app['candidate_id'])
                    await _callback(app['candidate_id'], app['id'])
                    await _matches(app['candidate_id'])
                    decided = await _auto(app['id'])
                    if demo:
                        await _guest_done(decided)
                elif source == 'phone' and number:
                    new = await store.save_new_caller(home, number, cfg, screen.answers, record['scorecard'],
                                                      job_id=screen.job['id'] if screen.job else None)
                    print('new caller stored as application', new['id'], flush=True)
                    await _contact(new['candidate_id'])
                    await _callback(new['candidate_id'], new['id'])
                    await _matches(new['candidate_id'])
                    await _auto(new['id'])
            except Exception as e:
                print('application update failed', app['id'] if app else number, e, flush=True)
        if traces:
            traces.force_flush()

    async def _contact(candidate_id: int):
        # An email given at the end of the screen, or a text with a link asked for instead (riley/screener.py)
        if not getattr(screen, 'contact', None) or demo:
            return
        try:
            await store.save_contact(home, candidate_id, screen.contact)
        except Exception as e:  # the screen is saved already; this never undoes it
            print('contact save failed', type(e).__name__, e, flush=True)

    async def _auto(application_id: int) -> dict | None:
        # 'immediate' mode (the demo): Dana's pre-written outcome is decided, approved and rung at once
        try:
            m = await outcome.auto(home, application_id)
            print('auto outcome', application_id, m and m['kind'], m and m.get('ring'), flush=True)
            return m
        except Exception as e:
            print('auto outcome failed', application_id, type(e).__name__, e, flush=True)
            return None

    async def _matches(candidate_id: int):
        # What they said about themselves goes into their facts, and each job they fully qualify for is a match event
        try:  # a failed match never stops the outcome that follows
            fits = await match.after_screen(guest.SANDBOX if demo else home, candidate_id, screen.cfg['questions'],
                                            screen.answers)
            print('matches', [f['job_id'] for f in fits], flush=True)
        except Exception as e:
            print('matches failed', candidate_id, type(e).__name__, e, flush=True)

    async def _guest_done(decided: dict | None):
        # Track 9: the guest's screen is in and _auto has run. When it approved and rang, that call says it all;
        # otherwise a short "you're in process" call goes back when a trunk is set
        assert demo is not None
        await guest.set_language(demo['candidate_id'], screen.language)
        if not guest.needs_status_call(decided):
            return  # a second call would cross the outcome call
        phone = (store._j_load(demo.get('guest')) or {}).get('phone') or store.phone_key(number)
        if phone:
            placed = await outbound.place(guest.e164(phone), {'mode': 'status', 'candidate_id': demo['candidate_id'],
                                                              'application_id': demo['application_id']},
                                          sandbox=guest.SANDBOX,
                                          consented=outbound.consented({**demo, 'sandbox': guest.SANDBOX}, phone))
            print('guest status call', placed, flush=True)

    if meta.get('mode') != 'status':  # the status call-back says one line and saves nothing
        ctx.add_shutdown_callback(_save_follow_up if follow else _save_outcome if out else _save_help if help
                                  else _save)

    async def _ended(_):
        state.ended_by = state.ended_by or 'agent ended the call'

    end = EndCallTool(
        extra_description='Call it once the goodbye is said, or when the caller wants to stop, has no time, '
                          'or is abusive.',
        end_instructions=END_WORDS, on_tool_called=_ended)

    def _language(lang: str):
        # Deepgram and Cartesia switch language mid-call; the Pro switches to its multilingual stack (Kokoro's
        # Spanish voice), loaded now so the swap runs while Riley thinks of the reply
        for part in [*ears, *chain_v]:
            # Spanish listening is Nova-3's multilingual mode, so "English, please" still gets through and the caller
            # can switch back (the owner's call, 7 Nov: Spanish-only listening heard one word in 45 s)
            if isinstance(part, deepgram.STT):
                part.update_options(language='multi' if lang == 'es' else 'en')
            elif isinstance(part, cartesia.TTS):
                part.update_options(language=lang)
            elif isinstance(part, (ProSpeechSTT, ProSpeechTTS)) and not route:
                part.switch(lang)
        if route:  # another language can mean another server (English on the Pro, the rest on the Windows PC)
            loading.add(task := asyncio.ensure_future(route.to(lang)))
            task.add_done_callback(loading.discard)
        elif pro:
            loading.add(task := asyncio.ensure_future(_load(pro)))
            task.add_done_callback(loading.discard)

    async def _load(pro: ProSpeechTTS):
        try:
            await pro.load()
        except Exception as e:  # the next line loads it, or the next voice takes over
            print('pro speech load failed:', type(e).__name__, e, flush=True)
    if meta.get('mode') == 'status':  # the short call-back after a guest's screen: one line, then hang up
        agent = guest_call.StatusCall(guest_call.status_line(cfg))
    elif follow:
        agent = followup.FollowUpLine(cfg, follow, who or {}, tools=[end])
    elif out:
        agent = outcomeline.OutcomeLine(cfg, out, who or {}, placed=meta.get('mode') in ('outcome', 'callback'),
                                        tools=[end])
    elif demo:
        agent = Screener(cfg, prefilled=prefilled, demo={'name': demo['name'], 'title': demo['title'],
                                                         'hospital': demo.get('hospital') or cfg['agent']['employer']},
                         outbound=bool(meta), on_language=_language, tools=[end])
    else:
        # A callback or a first screen is a call Riley placed: it opens with who is calling and why
        # (riley/identity.py hello)
        placed = 'callback' if callback else 'screen' if meta.get('mode') == 'screen' else None
        agent = (helpline.HelpLine(cfg, rows, help.tz, who=who, placed=placed, tools=[end]) if help else
                 Screener(cfg, greeting_audio=greeting_audio, prefilled=prefilled, carried=carried, who=who,
                          jobs=jobs, on_language=_language, placed=placed, tools=[end],
                          # a phone applicant with no email is asked for one, so they can use their page
                          ask_email=source == 'phone' and not (app or {}).get('email')))

    # The starter's noise cancellation: background noise and other voices are removed before Deepgram hears them
    await session.start(agent=agent, room=ctx.room, room_options=room_io.RoomOptions(
        audio_input=room_io.AudioInputOptions(
            noise_cancellation=ai_coustics.audio_enhancement(model=ai_coustics.EnhancerModel.QUAIL_VF_S)),
        delete_room_on_close=True))
