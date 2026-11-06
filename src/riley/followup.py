"""The follow-up call the scheduler places (riley/scheduler.py): a reminder and reconfirm request, a missed interview,
a moved date, or several of them at once when they fall on the same day.

Same order as every call Riley places to a number on file: the name and role check (riley/identity.py), nothing
before a yes. Then each update in the words the scheduler built from its records, one at a time. Riley never
decides anything here and never says a fact that is not in those words; a no-show is never asked why.
"""
import time
from dataclasses import dataclass, field

from livekit.agents import Agent, RunContext, function_tool
from livekit.agents.llm import ToolError

from . import identity, scheduler, store
from .screener import Screener, say


@dataclass
class FollowUp:
    cfg: dict
    items: list[dict]               # the events the call is for (scheduler.follow_up_items)
    lines: list[str]                # what Riley says, one per item, from the records
    started: float = field(default_factory=time.time)
    reconfirmed: bool | None = None  # their answer to the reminder: coming or not
    new_time: bool | None = None     # after a missed interview: would they like a new time
    recruiter_requested: str = ''
    went_quiet: int = 0
    checking_in: bool = False
    ended_by: str = ''
    identity: str = 'pending'       # always checked: an update is only for the person it is about


def kinds(state: FollowUp) -> set[str]:
    return {scheduler.kind_of(i) for i in state.items}


def instructions(cfg: dict, state: FollowUp, hidden: bool = False) -> str:
    a = cfg['agent']
    head = (f"You are {a['name']}, an AI assistant for {a['employer']} nursing recruitment, on a live phone call you "
            'placed. How you talk: every reply is spoken. One or two short sentences, no lists or symbols. Warm and '
            'plain. Never say which tool you are calling.')
    if hidden:  # nothing about them, not even why you called, until they confirm who they are
        return f'{head}\n\nWhat you know: none yet, they have not confirmed who they are.{identity.PENDING}'
    updates = '\n'.join(f'- {line}' for line in state.lines)
    todo = []
    if 'reminder' in kinds(state):
        todo.append('For the reminder, wait for their answer, then call reconfirm with whether they can make it. If '
                    f"they can't, say {a['recruiter']} will offer another time and call request_recruiter.")
    if 'no_show_follow_up' in kinds(state):
        todo.append("For the missed interview: say the update plainly and kindly, once, before you ask anything. Never "
                    "ask why they missed it or what happened. If they give a reason, say only \"thanks for letting me "
                    "know\": never say any of it back, not even softened (\"something came up\", \"at home\"), "
                    "since home, family and health are private. Ask whether they would like a new time and call "
                    "new_time with their answer.")
    todo.append(f"If they ask about a decision, say only what the updates say: {a['recruiter']} decides, and they "
                'will hear by the date given. Never guess, hint or promise an outcome.')
    todo.append(f"If they want a person, call request_recruiter: {a['recruiter']} will call them back.")
    todo.append('Then say goodbye and call end_call in a later reply than any question.')
    return f"""{head} You never decide, change or promise anything, and you add no fact to these updates.

The updates, word for word (one update per reply, in this order; wait for their answer before the next):
{updates}

What you do:
""" + '\n'.join(f'- {t}' for t in todo)


class FollowUpLine(Agent):
    llm_node = Screener.llm_node   # the same speech filter: no narration, no repeats, short turns
    whole_reply = True             # each reply under 60 words, the updates kept and said before its question (fit)
    private_reasons = True         # and no sentence says back why they missed it (screener.says_back)

    def __init__(self, cfg: dict, state: FollowUp, who: dict, **kw):
        full = instructions(cfg, state)
        # Code says the first update on the identity yes: a caller who brought up the missed interview first got a
        # new time logged with no "missed it, one more chance" ever said (hard callers no_show, 6 Nov)
        step = f'You have just said the first update, word for word: "{state.lines[0]}" Wait for their answer.'
        kw['tools'] = [*kw.get('tools', []), identity.tool(self, full, step, cfg['agent']['recruiter'],
                                                           on_yes=self.first_update, quiet=True)]
        super().__init__(instructions=instructions(cfg, state, hidden=True), **kw)
        self.cfg, self.state, self.who = cfg, state, who
        self.must_say = state.lines   # fit never cuts an update for length (Screener.llm_node)

    def opening(self) -> str:
        """Riley placed this call: who is calling, from where and why, then the name and role check."""
        return identity.greeting(self.cfg, self.who, placed='follow_up')

    async def on_enter(self) -> None:
        await say(self, self.opening())

    async def first_update(self) -> None:
        self.session.say(self.state.lines[0])

    @function_tool()
    async def reconfirm(self, context: RunContext[FollowUp], coming: bool) -> str:
        """Their answer to the interview reminder.

        Args:
            coming: true if they said they can make it
        """
        identity.check(self.state)
        if 'reminder' not in kinds(self.state):
            raise ToolError('there is no interview reminder on this call')
        context.userdata.reconfirmed = coming
        return 'kept. Never mention this tool.'

    @function_tool()
    async def new_time(self, context: RunContext[FollowUp], wanted: bool) -> str:
        """After a missed interview: whether they would like a new time.

        Args:
            wanted: true if they said yes
        """
        identity.check(self.state)
        if 'no_show_follow_up' not in kinds(self.state):
            raise ToolError('there is no missed interview on this call')
        context.userdata.new_time = wanted
        return (f"kept: {self.cfg['agent']['recruiter']} will offer a new time. Say so in one sentence. "
                'Never mention this tool.')

    @function_tool()
    async def request_recruiter(self, context: RunContext[FollowUp], reason: str) -> str:
        """The caller wants a person, or can't make the interview.

        Args:
            reason: why, in a few words
        """
        context.userdata.recruiter_requested = reason
        return f"flagged: {self.cfg['agent']['recruiter']} will call them back"


def who_for(app: dict) -> dict:
    """The name and role Riley checks first: the person on file and the role of the call's first update."""
    return {'name': app.get('name') or 'John Doe', 'title': app.get('title') or 'nursing',
            'applied': store.ts(app.get('applied'))}


async def save(sandbox: str, state: FollowUp) -> None:
    """After the call: one `follow_up_call` event, and when they confirmed it was them, what they answered:
    `reconfirmed` (riley/signals.py reads it), `not_coming`, `rebook_requested`, `recruiter_requested`."""
    first = state.items[0] if state.items else {}
    cid, aid = first.get('candidate_id'), first.get('application_id')
    await store.log(sandbox, 'follow_up_call', cid, aid, {
        'event_ids': [i['event_id'] for i in state.items], 'identity': state.identity,
        'reconfirmed': state.reconfirmed, 'new_time': state.new_time, 'recruiter_requested': state.recruiter_requested,
        'seconds': round(time.time() - state.started), 'ended_by': state.ended_by or 'call ended'})
    if state.identity != 'confirmed':
        return
    for i in state.items:
        k = scheduler.kind_of(i)
        if k == 'reminder' and state.reconfirmed is not None:
            await store.log(sandbox, 'reconfirmed' if state.reconfirmed else 'not_coming', i['candidate_id'],
                            i['application_id'], {'at': i['detail'].get('at')})
        if k == 'no_show_follow_up' and state.new_time:
            await store.log(sandbox, 'rebook_requested', i['candidate_id'], i['application_id'],
                            {'missed': i['detail'].get('at')})
    if state.recruiter_requested:
        await store.log(sandbox, 'recruiter_requested', cid, aid, {'reason': state.recruiter_requested})
