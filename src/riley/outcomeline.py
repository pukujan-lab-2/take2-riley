"""The outcome call: a caller with an approved message waiting hears it first (docs/BUILD-PLAN.md, track 4).

Same order every time: the name and role check (riley/identity.py), nothing before a yes; then Dana's recorded line in
her own voice; then Riley gives the message in short turns. Next round: Riley offers the listed times, with Dana or a
nurse interviewer, and `book_slot` books only one of those. Not yet or not selected: the reason, what would change it,
the other roles in the message (a yes adds that application), feedback, a person to review, and a second go at the
screen (`retake_screen`: the answers are cleared and Riley rings straight back to screen it afresh).

It never edits an answer, a score or a decision; a retake clears them so the next screen starts over. The message is
marked delivered at the end of a call in which the caller confirmed it was them.
"""
import contextlib
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

from livekit.agents import Agent, RunContext, function_tool
from livekit.agents.llm import ToolError

from . import identity, octo, outbound, outcome, store
from .screener import NO_LABELS, Screener, say

LANGS = {'en': 'English', 'es': 'Spanish'}


@dataclass
class Outcome:
    cfg: dict
    message: dict
    application: dict
    language: str | None = None     # None: ask English or Spanish first
    started: float = field(default_factory=time.time)
    booked: dict | None = None
    add_roles: list = field(default_factory=list)   # job ids from the message's offers
    requests: list = field(default_factory=list)    # [{kind, detail}] for Dana
    recruiter_requested: str = ''
    went_quiet: int = 0
    checking_in: bool = False
    ended_by: str = ''
    identity: str = 'pending'       # always checked: an outcome is only for the person it is about
    extra: list = field(default_factory=list)   # other updates due the same day, as said (riley/scheduler.py)
    retake: bool = False            # not yet or not selected: they asked to take the screen again (Riley rings back)


def parts(message: dict) -> dict:
    p = message.get('parts')
    return (json.loads(p) if isinstance(p, str) else p) or {}


def instructions(cfg: dict, message: dict, language: str | None, hidden: bool = False, extra=()) -> str:
    """With hidden=True, nothing about the outcome is in it: they have not confirmed who they are."""
    a, p = cfg['agent'], parts(message)
    if language in LANGS:
        speak = f'Reply only in {LANGS[language]}.'
    else:
        speak = ('You do not know their language yet: if they answer in Spanish, call set_language with es and reply '
                 'only in Spanish from then on; if in English, call set_language with en.')
    if hidden:
        facts = 'None yet: they have not confirmed who they are.' + identity.PENDING
    else:
        slots = '\n'.join(f"- {s['say']}" + (f" (only with {outcome.WITH[s['with']]})" if s.get('with') else '')
                          for s in p.get('slots', [])) or '- none listed'
        offers = '\n'.join(f"- [{o['job_id']}] {outcome.offer_text(o)}" for o in p.get('offers', [])) or '- none'
        facts = f"""The message {a['recruiter']} approved, word for word (say it in your own short turns, adding \
no fact):
{message['text']}

Open interview times (next round only; book only one of these):
{slots}

Other roles in the message (the number in brackets is for tools, never say it):
{offers}"""
        if extra:  # the scheduler combines one person's updates for the same day into this one call
            facts += ('\n\nOther updates for them today, word for word; give them after the message, before '
                      'goodbye:\n' + '\n'.join(f'- {e}' for e in extra))
    if hidden:  # nothing about the outcome, not even that there is one, until they confirm who they are
        return f"""You are {a['name']}, an AI assistant for {a['employer']} nursing recruitment, on a live phone call. \
{speak}

How you talk: every reply is spoken. One or two short sentences. Warm and plain. Never say which tool you are calling.

{facts}"""
    return f"""You are {a['name']}, an AI assistant for {a['employer']} nursing recruitment, on a live phone call with \
a nurse who applied. {a['recruiter']}, the recruiter, made a decision on their application and approved a message for \
them. You give it. You never change a decision, an answer or a score, and you never promise anything not in the \
message. {speak}

How you talk: every reply is spoken. One or two short sentences, no lists or symbols. Warm and plain. Never say which \
tool you are calling.

{facts}

What you do:
- Give the message in two or three short turns, in its order, checking they are with you.
- Next round: ask whether they would like the second interview with {a['recruiter']}, the recruiter, or with a nurse \
interviewer from the unit, and which of the open times works. Then call book_slot with the time exactly as listed \
and who it is with. Only a listed time; if none works, call request_recruiter.
- Not yet or not selected: the reason and what would change it, exactly as in the message. If the message has other \
roles, ask if they would like to be considered; a yes calls add_role with that role's number.
- Not yet or not selected, once the reason is given: offer once to run the screen again, in one sentence ("If you'd \
like, you can take the screen again now: I'll hang up and call you straight back."). A yes calls retake_screen; then \
say you are calling right back, say goodbye and call end_call.
- If they want to give feedback, ask if {a['recruiter']} may read it in their words, then call give_feedback.
- If they want a person to look at it again, call request_recruiter.
- If they are upset or say it is unfair, take it seriously in one short sentence of plain words ("that's hard to \
hear", "it's fair to ask"), vary it, and {NO_LABELS}. Never name a feeling they did not name. Then say what you can \
do: what would change it, the other roles, feedback, or {a['recruiter']} looking at it again.
- Then say goodbye and call end_call in a later reply than any question."""


class OutcomeLine(Agent):
    llm_node = Screener.llm_node   # the same speech filter: no narration, no repeats, short turns
    whole_reply = True             # and each reply stays under 60 words, its question included (screener.fit)

    def __init__(self, cfg: dict, state: Outcome, who: dict,
                 booker: Callable[[int, str, str], Awaitable[dict]] = outcome.book, placed: bool = False, **kw):
        m = state.message
        full = instructions(cfg, m, state.language, extra=state.extra)
        step = ("Now give the message's first part in one or two sentences, then check they are with you."
                if m['kind'] != 'next_round' else
                'Now say they are moving on to a second interview, then ask whether they would like it with the '
                'recruiter or a nurse interviewer.')
        kw['tools'] = [*kw.get('tools', []), identity.tool(self, full, step, cfg['agent']['recruiter'], self._dana)]
        super().__init__(instructions=instructions(cfg, m, state.language, hidden=True), **kw)
        self.cfg, self.state, self.who, self.booker = cfg, state, who, booker
        self.placed = placed   # Riley rang them for this message (outcome.call_now, the scheduler), or they called in
        self.p = parts(m)
        self.offer_ids = {int(o['job_id']) for o in self.p.get('offers', []) if o.get('job_id') is not None}

    def opening(self) -> str:
        hello = identity.greeting(self.cfg, self.who, placed='outcome' if self.placed else None)
        if not self.state.language:
            hello += ' Si prefiere hablar en español, dígamelo.'
        return hello

    async def on_enter(self) -> None:
        await say(self, self.opening())

    async def _dana(self) -> None:
        """Right after a yes: Dana's recorded line in her voice, before Riley says anything about the outcome. In
        Spanish, or with no clip kept, Riley goes straight on."""
        if self.state.language == 'es' or not self.session.tts:
            return
        frames = outcome.clip_frames(self.state.message['kind'], self.cfg)
        if not frames:
            return

        async def audio():
            for f in frames:
                yield f
        text = outcome.dana_lines(self.cfg).get(self.state.message['kind'], '')
        self.session.say(f'(Dana) {text}', audio=audio(), allow_interruptions=False)

    def _ok(self) -> None:
        identity.check(self.state)

    @function_tool()
    async def set_language(self, context: RunContext[Outcome], language: Literal['en', 'es']) -> str:
        """The language the caller speaks.

        Args:
            language: en for English, es for Spanish
        """
        context.userdata.language = language
        full = instructions(self.cfg, self.state.message, language, hidden=self.state.identity != 'confirmed',
                            extra=self.state.extra)
        await self.update_instructions(full)
        return f'reply only in {LANGS[language]} from now on. Never mention this tool.'

    @function_tool()
    async def book_slot(self, context: RunContext[Outcome], slot: str, with_whom: Literal['recruiter', 'nurse']) -> str:
        """Book the second interview at one of the listed open times.

        Args:
            slot: the time exactly as listed, e.g. Monday, November 9, at 10 am
            with_whom: recruiter for Dana, nurse for a nurse interviewer from the unit
        """
        self._ok()
        if self.state.message['kind'] != 'next_round':
            raise ToolError('only a next-round message books an interview')
        if context.userdata.booked:
            return f"already booked for {context.userdata.booked['say']}. Never mention this tool."
        chosen = outcome.pick_slot(self.p.get('slots', []), slot, with_whom)
        if not chosen:
            listed = '; '.join(s['say'] for s in self.p.get('slots', []))
            return f'not one of the open times. Offer only these: {listed}.'
        result = await self.booker(self.state.message['id'], chosen['say'], with_whom)
        if not result.get('booked'):
            return f"could not book: {result.get('why')}. Offer another listed time, or request_recruiter."
        context.userdata.booked = result
        print('  outcome booked', result.get('say'), with_whom, flush=True)
        return (f"booked: {chosen['say']} with {outcome.WITH[with_whom]}. Confirm it back in one sentence. "
                'Never mention this tool.')

    @function_tool()
    async def add_role(self, context: RunContext[Outcome], job_id: int) -> str:
        """They want to be considered for one of the other roles in the message.

        Args:
            job_id: the number in brackets of that role
        """
        self._ok()
        if job_id not in self.offer_ids:
            raise ToolError(f'only a role in the message: one of {sorted(self.offer_ids)}')
        context.userdata.add_roles.append(job_id)
        return 'added; a short call covers only what differs for that role. Never mention this tool.'

    @function_tool()
    async def retake_screen(self, context: RunContext[Outcome]) -> str:
        """They want to take the screen again (not yet or not selected only). Riley rings straight back after
        this call and asks every question afresh; the answers they gave before are cleared."""
        self._ok()
        if self.state.message['kind'] == 'next_round':
            raise ToolError('only after a not yet or not selected outcome')
        context.userdata.retake = True
        return 'set: Riley calls back once this call ends. Say so, say goodbye, call end_call. Never mention this tool.'

    @function_tool()
    async def give_feedback(self, context: RunContext[Outcome], words: str, recruiter_may_read: bool) -> str:
        """The caller's feedback, in their own words.

        Args:
            words: what they said, close to their words
            recruiter_may_read: whether they said the recruiter may read it
        """
        self._ok()
        context.userdata.requests.append({'kind': 'feedback', 'detail': {'words': words, 'share': recruiter_may_read}})
        return 'kept. Never mention this tool.'

    @function_tool()
    async def request_recruiter(self, context: RunContext[Outcome], reason: str) -> str:
        """The caller wants a person to look at it again, or to talk to one.

        Args:
            reason: why, in a few words
        """
        context.userdata.recruiter_requested = reason
        return f"flagged: {self.cfg['agent']['recruiter']} will call them back"


async def lookup(sandbox: str, number: str, metadata: str | None) -> tuple[dict, dict] | None:
    """(message, application) when this call should give an outcome: an outbound call placed for one
    (room metadata mode 'outcome'), or a known number with an approved message waiting."""
    meta = {}
    with contextlib.suppress(ValueError):
        meta = json.loads(metadata) if metadata else {}
    m = None
    if meta.get('mode') == 'outcome' and meta.get('message_id'):
        m = await outcome.message(int(meta['message_id']))
        m = m if outcome.can_deliver(m) else None
    elif number:
        who = await store.find_candidate(sandbox, phone=number)
        m = await outcome.pending(sandbox, who['id']) if who else None
    if not m:
        return None
    return m, await outcome.application(m['application_id'])


async def save(sandbox: str, state: Outcome) -> None:
    """After the call: delivered if they confirmed it was them, any added roles, and one event per request. Never
    answers or score."""
    app = state.application
    await store.log(sandbox, 'outcome_call', app['candidate_id'], app['id'], {
        'message_id': state.message['id'], 'identity': state.identity, 'booked': state.booked,
        'seconds': round(time.time() - state.started), 'recruiter_requested': state.recruiter_requested,
        'ended_by': state.ended_by or 'call ended', 'language': state.language})
    if state.identity != 'confirmed':
        return
    await outcome.deliver(state.message['id'])
    for job_id in state.add_roles:
        await store.apply(sandbox, app['candidate_id'], job_id, source='outcome call')
    for r in state.requests:
        await store.log(sandbox, r['kind'], app['candidate_id'], app['id'], r['detail'])
    if state.retake:
        await retake(sandbox, app)


async def retake(sandbox: str, app: dict) -> dict:
    """A second go at the same screen: the answers, score and decision are cleared and the application is back to
    applied, so the call Riley places now screens it afresh. Logged, so the tracker shows it was retaken."""
    await octo.query("update applications set answers = '{}'::jsonb, score = null, decision = null, "
                     "status = 'applied', updated = now() where id = $1", [app['id']])
    # A /try guest is screened as a guest again (made-up name and role); anyone else as a callback on their number
    meta = {'mode': 'guest' if app.get('guest') else 'callback', 'candidate_id': int(app['candidate_id']),
            'application_id': int(app['id'])}
    phone = app.get('phone') or ''
    placed = await outbound.place(phone, meta) if phone else {'placed': False, 'why': 'no phone on file'}
    await store.log(sandbox, 'retake', app['candidate_id'], app['id'], {**meta, **placed})
    return placed
