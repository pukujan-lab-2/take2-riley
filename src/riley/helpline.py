"""The help line: a known number that is not mid-screen hears where each application stands (SPEC §3, help line).

Nothing here can change an answer or a score. The agent has no record_answer, and what the caller asks for (a fixed
fact, a referral, another role, feedback, a withdrawal) is written to `events` for Dana, or to the withdrawal fields,
never to `applications.answers` or `applications.score`. Every date comes from the store; the model never works one out.
"""
import time
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from livekit.agents import Agent, RunContext, function_tool
from livekit.agents.llm import ToolError

from . import identity, store
from .identity import day
from .screener import Screener, say

# What each status means to the caller, and the next step after it
STATUS = {
    'applied': ('received; the phone screen is not done yet', 'the five-minute screen, by phone or text'),
    'screened': ('screened; {recruiter} is reading it', "{recruiter}'s decision"),
    'interview': ('moved to an interview with {recruiter}', 'the interview'),
    'not_yet': ('not moving forward right now', 'nothing more on this one unless something changes'),
    'not_selected': ('not selected', 'nothing more on this one'),
    'withdrawn': ('withdrawn at your request', 'nothing, unless you undo it'),
}
OPTIONS = ('check your status any time, ask for a person, ask for another way to do this, be considered for other '
           'roles, give feedback, or fix a fact')


def when(dt: datetime | None, tz: str = 'America/New_York') -> str:
    """A promised date the way Riley says it: "Friday, November 7, by 3 pm"."""
    if not dt:
        return 'no date yet'
    d = dt.astimezone(ZoneInfo(tz))
    hour = d.strftime('%I').lstrip('0') + (f":{d.minute:02d}" if d.minute else '') + (' am' if d.hour < 12 else ' pm')
    return f"{d.strftime('%A, %B')} {d.day}, by {hour}"


def lines(rows: list[dict], recruiter: str, tz: str = 'America/New_York') -> str:
    """One line per application, with only this caller's own facts and each job's applicant count."""
    out = []
    for r in rows:
        stands, nxt = (s.format(recruiter=recruiter) for s in STATUS.get(r['status'], (r['status'], 'an update')))
        hear = 'no date: withdrawn' if r['status'] == 'withdrawn' else when(store.ts(r.get('promised_by')), tz)
        out.append(f"- [{r['id']}] {r['title']}, {r['location']}: applied {day(store.ts(r['applied']), tz)}; "
                   f"{stands}; next step: {nxt}; they hear by {hear}; "
                   f"applicants for this job so far: {r['applicants']}"
                   + (f"; why: {r['why']}" if r.get('why') else ''))
    return '\n'.join(out)


@dataclass
class Help:
    cfg: dict
    rows: list
    tz: str = 'America/New_York'
    started: float = field(default_factory=time.time)
    asked: list = field(default_factory=list)       # what the caller wanted, for the event log
    requests: list = field(default_factory=list)    # [{kind, application_id, detail}] for Dana
    withdraw: list = field(default_factory=list)    # application ids
    undo: list = field(default_factory=list)
    recruiter_requested: str = ''
    went_quiet: int = 0
    checking_in: bool = False
    ended_by: str = ''
    identity: str = 'none'    # as on Screen: 'pending' until a known caller confirms the name and role


def instructions(cfg: dict, rows: list[dict], tz: str = 'America/New_York', hidden: bool = False) -> str:
    """With hidden=True, nothing about their applications is in it yet: the caller has not confirmed who they are."""
    a = cfg['agent']
    apps = ('None yet: they have not confirmed who they are.' + identity.PENDING if hidden
            else lines(rows, a['recruiter'], tz))
    return f"""You are {a['name']}, an AI assistant for {a['employer']} nursing recruitment, on a live phone call with \
a nurse who already applied. This is the help line: it never changes their answers or their score, and you say so if \
they worry about it. You do not screen them and you ask no screening questions.

How you talk: every reply is spoken. One or two short sentences, no lists or symbols, dates said the way a person \
would. Warm and plain. Everything you write is spoken to the caller, so never describe them in the third person or say \
which tool you are calling.

Their applications, the only facts you have about them (the number in brackets is for tools, never say it):
{apps}

What you do:
- Start by telling them where each application stands and the day and time they will hear, in one or two sentences. \
For several applications, name each role briefly. Then ask what they need.
- Say dates and times exactly as written above; never move, round or guess one.
- The applicant count only if they ask, and only as written above. Never say anything about any other candidate.
- You never say whether they will get the job, never give a score, and never promise an interview.
- Not yet or not selected: give the why written for it in the same turn as the status, in one or two sentences, \
with what they said on the call ("On the call you said one year as an RN; this role needs two."). Where it says \
their resume showed otherwise, say that plainly, never as a mistake or a lie. Then ask once if any of it is wrong.
- If they want to fix a fact (for example a certification or their years), or say a detail in the why is wrong, \
call fix_fact; tell them {a['recruiter']} will see it and it reopens the review of that application.
- If they forgot a referral, call add_referral with the person's name and unit; {a['recruiter']} confirms it with the \
employee.
- If they want another role considered, call add_role; a short call covers only what differs for that role.
- If they want to withdraw one application, check which one, then call withdraw; the others stay as they are. If they \
want a withdrawal undone, call undo_withdraw.
- If they want to give feedback, ask if {a['recruiter']} may read it in their words, then call give_feedback.
- If they want a person, call request_recruiter and tell them {a['recruiter']} will call back.
- Before the goodbye, say in one sentence that they can {OPTIONS}, any time on this number. Then say goodbye and call \
end_call in a later reply than any question."""


class HelpLine(Agent):
    llm_node = Screener.llm_node   # the same speech filter: no narration, no repeats, short turns

    def __init__(self, cfg: dict, rows: list[dict], tz: str = 'America/New_York', who: dict | None = None,
                 placed: str | None = None, **kw):
        if who:  # nothing about them until they confirm the name and role (riley/identity.py)
            step = 'Now tell them where each application stands, in one or two sentences, then ask what they need.'
            kw['tools'] = [*kw.get('tools', []),
                           identity.tool(self, instructions(cfg, rows, tz), step, cfg['agent']['recruiter'])]
        super().__init__(instructions=instructions(cfg, rows, tz, hidden=bool(who)), **kw)
        self.cfg, self.ids, self.who, self.tz = cfg, {r['id'] for r in rows}, who, tz
        self.rows = {r['id']: r for r in rows}
        self.placed = placed  # a callback Riley placed opens with who is calling and why, not "thanks for calling"

    def opening(self) -> str | None:
        """The first line, when it is a fixed one (a number on file); else the model greets them."""
        return identity.greeting(self.cfg, self.who, self.tz, self.placed) if self.who else None

    async def on_enter(self) -> None:
        if line := self.opening():
            await say(self, line)
            return
        self.session.generate_reply(instructions='Greet them as the help line in a few words, then tell them where '
                                                 'their applications stand.')

    def _check(self, application_id: int) -> None:
        identity.check(self.session.userdata)
        if application_id not in self.ids:
            raise ToolError(f'not one of their applications; use one of {sorted(self.ids)}')

    def _ask(self, context: RunContext[Help], kind: str, application_id: int | None, **detail) -> None:
        identity.check(context.userdata)
        context.userdata.requests.append({'kind': kind, 'application_id': application_id, 'detail': detail})
        print(f'  help {kind:14} {application_id} {detail}', flush=True)

    @function_tool()
    async def fix_fact(self, context: RunContext[Help], application_id: int, item: str, now_says: str) -> str:
        """The caller says something we have is wrong. Sent to the recruiter, and that application's review reopens.

        Args:
            application_id: the number in brackets of the application it is about
            item: what the fact is about, e.g. certifications or years as an RN
            now_says: what they say is right, in a few job-related words
        """
        self._check(application_id)
        self._ask(context, 'fix', application_id, item=item, now_says=now_says)
        return 'sent to the recruiter; the review of that application reopens. Never mention this tool.'

    @function_tool()
    async def add_referral(self, context: RunContext[Help], application_id: int, name: str, unit: str) -> str:
        """The caller was referred by an employee and forgot to say so.

        Args:
            application_id: the application it is for
            name: the employee's name
            unit: their unit, e.g. 5 West
        """
        self._check(application_id)
        self._ask(context, 'referral', application_id, name=name, unit=unit)
        return 'added; the recruiter confirms it with the employee. Never mention this tool.'

    @function_tool()
    async def add_role(self, context: RunContext[Help], role: str) -> str:
        """The caller wants to be considered for another role.

        Args:
            role: the role, unit, shift or location they asked about, as they said it
        """
        self._ask(context, 'add_role', None, role=role)
        return 'noted; a short call covers only what differs for that role. Never mention this tool.'

    @function_tool()
    async def withdraw(self, context: RunContext[Help], application_id: int) -> str:
        """The caller wants to withdraw one application. The others are not touched.

        Args:
            application_id: the application to withdraw
        """
        self._check(application_id)
        context.userdata.withdraw.append(application_id)
        return 'withdrawn; they can undo it within 7 days on this number. Never mention this tool.'

    @function_tool()
    async def undo_withdraw(self, context: RunContext[Help], application_id: int) -> str:
        """The caller wants a withdrawal undone.

        Args:
            application_id: the withdrawn application
        """
        self._check(application_id)
        r = self.rows[application_id]
        if not store.can_undo(store.ts(r.get('withdrawn')), datetime.now(tz=ZoneInfo('UTC'))):
            return 'cannot undo: it was not withdrawn, or more than 7 days ago. Offer to have the recruiter call them.'
        context.userdata.undo.append(application_id)
        return 'undone; it is back where it was. Never mention this tool.'

    @function_tool()
    async def give_feedback(self, context: RunContext[Help], words: str, recruiter_may_read: bool) -> str:
        """The caller's feedback, in their own words.

        Args:
            words: what they said, close to their words
            recruiter_may_read: whether they said the recruiter may read it
        """
        self._ask(context, 'feedback', None, words=words, share=recruiter_may_read)
        return 'kept. Never mention this tool.'

    @function_tool()
    async def request_recruiter(self, context: RunContext[Help], reason: str) -> str:
        """The caller wants to talk to a person.

        Args:
            reason: why, in a few words
        """
        context.userdata.recruiter_requested = reason
        return f"flagged: {self.cfg['agent']['recruiter']} will call them back"


async def save(sandbox: str, candidate_id: int | None, help: Help) -> None:
    """After a help call: one 'help' event, one event per request, and any withdrawal. Never answers or score."""
    now = datetime.now(tz=ZoneInfo('UTC'))
    await store.log(sandbox, 'help', candidate_id, None, {
        'seconds': round(time.time() - help.started), 'requests': len(help.requests),
        'recruiter_requested': help.recruiter_requested, 'ended_by': help.ended_by or 'call ended'})
    for r in help.requests:
        await store.log(sandbox, r['kind'], candidate_id, r['application_id'], r['detail'])
        if r['kind'] == 'fix' and r['application_id']:
            await store.reopen(r['application_id'])
    for a in help.withdraw:
        await store.withdraw(a, now)
    for a in help.undo:
        await store.undo_withdraw(a, now)
