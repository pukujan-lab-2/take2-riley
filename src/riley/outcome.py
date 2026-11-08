"""Outcomes: Dana's decision becomes a message built from the records, she approves it, and it is delivered on a call
(docs/BUILD-PLAN.md, track 4).

  decide ──► draft message (parts from records, Riley joins them, a fact check in code) ──► approve ──► ring or queue
  ──► the outcome call (riley/outcomeline.py) gives it ──► delivered, and the application's status moves

Dana's words come from `criteria.reason` and `criteria.change`; the other roles come from matching (riley/match.py),
never from the model. Riley only joins the parts into sentences, and `check()` refuses a draft that names a role, a
date, a number or an item that is not in its parts; then the plain template join is used instead.

How outcomes run is one setting per sandbox (`settings`, key `outcome_mode`):
  immediate  the demo: a screen ends, `auto()` decides from Dana's pre-written reasons and approves; the scheduler
             rings dana.ring_after_minutes later, and until then their own call gets the help line
  normal     Dana decides in the tracker; the message waits for their next call, or the scheduler outside quiet hours
  dana_now   Dana decides; the moment she approves, the call rings
Every ring goes through `approve()` or `call_now()`, so no draft is ever rung, and every ring writes an event.
Nothing here changes an answer or a score.
"""
import contextlib
import html
import json
import os
import re
from datetime import UTC, datetime, time, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from . import bias, octo, store, tenant
from .helpline import when
from .identity import day

ROOT = Path(__file__).resolve().parents[2]
KINDS = ('next_round', 'not_yet', 'not_selected')
# The application's status once the message is delivered (riley/helpline.py STATUS reads these)
STATUS_AFTER = {'next_round': 'interview', 'not_yet': 'not_yet', 'not_selected': 'not_selected'}
MODES = ('immediate', 'normal', 'dana_now')
DEFAULT_MODE = 'immediate'   # the demo and Hudson Park (docs/BUILD-PLAN.md, "Outcome mode")
ORG_MODE = 'normal'          # a real employer's org (org_<id>): Dana decides, nothing rings on approval (gap 12)
WITH = {'recruiter': 'Dana, the recruiter', 'nurse': 'a nurse interviewer from the unit'}
QUIET_START, QUIET_END = time(21), time(8)   # no outcome call between 9 pm and 8 am, their time
AUTO_BY = 'dana (auto, demo)'
FROM_ADDRESS = 'no-reply@riley.design-bakery.com'   # an org's .eml sender address until orgs send their own mail

# Words the fact check looks for: a role, unit, shift or item named in the text must be in the parts.
ITEMS = ['rn', 'lpn', 'cna', 'pct', 'bls', 'acls', 'pals', 'nrp', 'ccrn', 'tncc', 'ekg', 'iv', 'icu', 'micu', 'ed',
         'l&d', 'labor and delivery', 'emergency', 'telemetry', 'step-down', 'med-surg', 'medical-surgical',
         'pediatrics', 'peds', 'rehab', 'telehealth', 'triage', 'float', 'per diem', 'phlebotomy', 'fetal monitoring',
         'compact license', 'long-term care', 'patient care technician', 'nights', 'evenings', 'weekends',
         'manhattan', 'bronx', 'brooklyn', 'queens', 'midtown', 'virtual care', 'float pool']
WEEKDAYS = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
MONTHS = ['january', 'february', 'march', 'april', 'may', 'june', 'july', 'august', 'september', 'october',
          'november', 'december']
NUMBER = re.compile(r'\b\d+(?:[.:]\d+)?\b')
HOSPITAL = re.compile(r'\bHudson Park(?: [A-Z][a-z]+)?')
# A message never closes the door on other roles: with none listed, Dana can still consider them as roles open
CLOSED = re.compile(r"\b(?:no|not any|aren'?t any|don'?t (?:currently |yet )?have(?: any)?) (?:other |more )?"
                    r"(?:open(?:ings|\s+roles)?|roles|positions|jobs)\b|\b(?:can'?t|cannot|can not|won'?t|unable to|"
                    r"not able to)(?: be)? consider(?:ed)?\b", re.I)
OPEN_DOOR = "If you'd like to be considered for other roles as they open, say so on this number."


def config() -> dict:
    return yaml.safe_load((ROOT / 'screen.yaml').read_text())


# ---- rules, without a database ----

def can_deliver(message: dict) -> bool:
    """Nothing reaches a candidate without Dana's approval (rule 1): only an approved, undelivered message."""
    return message.get('status') == 'approved' and not message.get('delivered')


def rings_now(mode: str, message: dict) -> bool:
    """Whether approving rings the candidate at once. Never a draft; in 'normal' never (it waits for their call or
    the scheduler)."""
    return can_deliver(message) and mode in ('immediate', 'dana_now')


def ring_after(message: dict, now: datetime | None = None) -> datetime | None:
    """When an auto outcome may first be given (screen.yaml dana.ring_after_minutes), while that is still ahead.
    Until then their own call gets the help line, and the scheduler rings at that time."""
    at = (_load(message.get('parts')) or {}).get('ring_after')
    if not at:
        return None
    t = datetime.fromisoformat(at)
    return t if t > (now or datetime.now(UTC)) else None


def auto_decides(mode: str) -> bool:
    return mode == 'immediate'


def quiet(now: datetime, tz: str = 'America/New_York') -> bool:
    t = now.astimezone(ZoneInfo(tz)).time()
    return t >= QUIET_START or t < QUIET_END


def next_allowed(now: datetime, tz: str = 'America/New_York') -> datetime:
    """The first moment outside quiet hours, their time: now if it already is."""
    if not quiet(now, tz):
        return now
    local = now.astimezone(ZoneInfo(tz))
    morning = datetime.combine(local.date(), QUIET_END, tzinfo=ZoneInfo(tz))
    return morning if local.time() < QUIET_END else morning + timedelta(days=1)


def say_time(dt: datetime, tz: str = 'America/New_York') -> str:
    """"Monday, November 9, at 10 am": a slot the way Riley says it."""
    return when(dt, tz).replace(', by ', ', at ')


def slot_list(raw, now: datetime | None = None, tz: str = 'America/New_York', limit: int = 3) -> list[dict]:
    """The job's open times as [{at, say, with}], future ones only, soonest first. A slot is an ISO time, or
    {at, with} when it is only with the recruiter or only with a nurse interviewer, and {.., interviewer_id} when it
    was opened for one interviewer."""
    now = now or datetime.now(UTC)
    out = []
    for s in raw or []:
        at, who = (s.get('at'), s.get('with')) if isinstance(s, dict) else (s, None)
        dt = store.ts(at)
        if isinstance(dt, datetime) and dt > now:
            given = s.get('interviewer_id') if isinstance(s, dict) else None
            out.append({'at': dt.isoformat(), 'say': say_time(dt, tz), 'with': who,
                        **({'interviewer_id': given} if given is not None else {})})
    return sorted(out, key=lambda s: s['at'])[:limit]


def _norm(s: str) -> str:
    return re.sub(r'[^a-z0-9]+', ' ', s.lower()).strip()


def pick_slot(slots: list[dict], slot: str, with_whom: str | None = None) -> dict | None:
    """The listed slot they chose, matched on how it was said or its ISO time; None for anything not on the list,
    or with someone that slot is not open for."""
    want = _norm(slot)
    for s in slots:
        if want and want in (_norm(s['say']), _norm(s['at'])) and (not s.get('with') or s['with'] == with_whom):
            return s
    return None


def option_text(o) -> str:
    """One of the options Dana allows on a job (another shift, per diem, another site), as words."""
    if isinstance(o, dict):
        return str(o.get('say') or o.get('label') or o.get('note') or o.get('kind') or '')
    return str(o)


def offer_text(o: dict) -> str:
    bits = [o.get('title'), o.get('hospital'), o.get('shift')]
    return ', '.join(str(b) for b in bits if b)


# Which screen answer speaks to each kind of criterion (riley/match.py targets); a shift is the answer named after it
ANSWER_FOR = {'license': ('license',), 'cert': ('certifications',), 'years': ('years_rn',),
              'unit': ('acute_care', 'units'), 'units': ('acute_care', 'units')}


def said_for(criterion: dict, answers: dict, resume: dict) -> dict:
    """For one item that fell short: `heard`, what they said on the call about it, word for word as recorded, so the
    reason is theirs and not a form line; and `resume_differs` when their resume met it but the call did not, the
    contradiction they are asked to settle (the review reopens if the resume is right)."""
    target = criterion.get('target') or {}
    if len(target) != 1:
        return {'heard': '', 'resume_differs': False}
    (kind, want), = target.items()
    keys = ((re.sub(r'[^a-z0-9]+', '_', str(want).lower()).strip('_'), 'nights', 'days', 'shift')
            if kind == 'shift' else ANSWER_FOR.get(kind, ()))
    got = next((answers[k] for k in keys if isinstance(answers.get(k), dict) and answers[k].get('value')), None)
    heard = str(got['value']) if got else ''
    on_resume = 'unknown'
    match = track('match')
    if match and resume:
        with contextlib.suppress(Exception):
            on_resume = match._item(resume, target)[0]
    return {'heard': heard, 'resume_differs': bool(got and got.get('meets') == 'no' and on_resume == 'met')}


def parts_for(result: str, reason: str, criteria: list[dict], offers: list[dict], job: dict, name: str,
              cfg: dict, now: datetime | None = None, tz: str = 'America/New_York', answers: dict | None = None,
              resume: dict | None = None) -> dict:
    """Everything the message may say, each from a record: Dana's result and reason, her words for each item that fell
    short with what they said about it on the call (and whether their resume said otherwise), the other roles from
    matching, the job's options, her open times, and the next step with its date."""
    if result not in KINDS:
        raise ValueError(f'result must be one of {KINDS}')
    now = now or datetime.now(UTC)
    dana = cfg.get('dana', {})
    items = [{'id': c.get('id'), 'name': c.get('name'), 'reason': c.get('reason') or '',
              'change': c.get('change') or '', **said_for(c, answers or {}, resume or {})}
             for c in criteria]
    slots = slot_list(job.get('slots'), now, tz) if result == 'next_round' else []
    if result == 'next_round':
        nxt = 'pick one of the times, and who you would like to meet'
    elif result == 'not_yet':
        nxt = 'your answers and your date are kept; call this number when that changes'
    else:
        nxt = 'nothing more on this role'
    return {
        'result': result, 'name': name or '', 'role': job.get('title') or '',
        'hospital': job.get('hospital') or job.get('location') or '',
        'reason': reason or '', 'items': items,
        'offers': [{k: o.get(k) for k in ('job_id', 'title', 'hospital', 'unit', 'shift')} for o in offers or []],
        'options': ([t for t in (option_text(o) for o in job.get('options') or []) if t]
                    if result != 'next_round' else []),
        'slots': slots, 'next_step': nxt, 'decided': day(now, tz),
        'recruiter': cfg['agent']['recruiter'], 'employer': dana.get('employer', cfg['agent']['employer']),
    }


def _blob(parts: dict) -> str:
    return json.dumps(parts, default=str).lower()


def check(text: str, parts: dict) -> list[str]:
    """What the text names that its parts don't: a role, unit or item, a hospital, a day, a month or a number.
    An empty list means every fact in it comes from the records."""
    blob, low = _blob(parts), text.lower()
    problems = []
    for t in ITEMS:
        pat = rf'(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])'
        if re.search(pat, low) and not re.search(pat, blob):
            problems.append(t)
    for w in WEEKDAYS + MONTHS:
        if re.search(rf'\b{w}\b', low) and w not in blob:
            problems.append(w)
    nums = set(NUMBER.findall(blob))
    problems += [n for n in NUMBER.findall(low) if n not in nums]
    problems += [h for h in (m.group(0).lower() for m in HOSPITAL.finditer(text)) if h not in blob]
    if CLOSED.search(text):
        problems.append('says no other roles')
    return sorted(set(problems))


def template(parts: dict, first_person: bool = False) -> str:
    """The plain join of the parts, used when the model is not reachable or its draft fails the check, and for Dana's
    email (first_person: in her words, "I'd like to..."). Its glue words name no role, date, number or item, so it
    always passes `check()`."""
    p, who = parts, parts['recruiter']
    me = (lambda first, riley: first) if first_person else (lambda first, riley: riley)
    first = p['name'].split()[0] if p['name'] else ''
    out = [(f'Hi {first}. ' if first else 'Hi. ') + f"This is about the {p['role']} role"
           + (f" at {p['hospital']}." if p['hospital'] else '.')]
    if p['result'] == 'next_round':
        out.append(me("Good news: I'd like to move you forward to a second interview.",
                      f'Good news: {who} would like to move you forward to a second interview.'))
        if p['reason']:
            out.append(p['reason'])
        if p['slots']:
            times = [s['say'] + (' (with a nurse interviewer)' if s.get('with') == 'nurse' else
                                 f' (with {who})' if s.get('with') == 'recruiter' else '') for s in p['slots']]
            listed = times[0] if len(times) == 1 else ', '.join(times[:-1]) + f' or {times[-1]}'
            out.append(me('You can meet me, or a nurse interviewer from the unit.',
                          f'You can meet {who}, the recruiter, or a nurse interviewer from the unit.')
                       + f' The open times are {listed}.')
        else:
            out.append(me("I'll send you times for it.", f'{who} will send you times for it.'))
    else:
        if p['result'] == 'not_yet':
            out.append(me("I'm not moving this one forward right now.",
                          f"{who} isn't moving this one forward right now."))
        else:
            out.append(me("I've decided not to move forward with this one.",
                          f'{who} has decided not to move forward with this one.'))
        if p['reason']:
            out.append(p['reason'])
        for i in p['items']:
            if i.get('heard') and i.get('resume_differs'):
                out.append(f"Your resume showed {i['name']}, but on the call you said: {i['heard']}.")
            elif i.get('heard'):
                out.append(f"On the call you said: {i['heard']}.")
            if i['reason'] and i['reason'] not in p['reason']:
                out.append(i['reason'])
        out += [i['change'] for i in p['items'] if i['change']]
        if any(i.get('heard') for i in p['items']):
            out.append('If any of that is wrong, tell us on this number and the review reopens.')
        if p['offers']:
            roles = '; '.join(offer_text(o) for o in p['offers'])
            out.append(f'Another role fits what you told us: {roles}. Say yes and you are considered for it too.')
        else:
            out.append(OPEN_DOOR)
        if p['options']:
            out.append(me('I can also offer', f'{who} can also offer') + f": {'; '.join(p['options'])}.")
        out.append('You can give feedback, or ask for a person to look at this again, any time on this number.')
    out.append(f"Next step: {p['next_step']}.")
    return ' '.join(s.strip() for s in out if s.strip())


DRAFT = """You write one short outcome message from a hospital recruiter's assistant to a nurse applicant. \
Join the parts below into natural, warm, plain sentences, in this order: the result, the reason, what would change \
it, the other roles, how to give feedback or ask a person to review, and the next step. For each item, give the \
reason with what they said on the call ("heard", in their words); where "resume_differs" is true, say plainly that \
their resume showed it but the call did not, never as a mistake or a lie. When any item has "heard", end the reason \
with one sentence that if any of it is wrong they can say so on this number and the review reopens. Use only the \
facts in the parts: add no role, unit, certification, hospital, day, date, time or number that is not in them, and \
keep the recruiter's reason and change in her own words. When "offers" is empty, never say there are no other \
openings or that they can't be considered: say they can ask on this number to be considered for other roles as they \
open. No greeting line, no sign-off, no lists. Under 110 words.

Parts (JSON):
"""


async def draft(parts: dict, cfg: dict) -> tuple[str, str]:
    """(text, how): Riley's joined draft if a model answers and its text passes the check, else the template. Tries
    screen.yaml dana.draft_model, then the rest of the brain chain."""
    import asyncio

    from livekit.agents import llm as lk_llm

    from . import brain
    ctx = lk_llm.ChatContext()
    ctx.add_message(role='user', content=DRAFT + json.dumps(parts, default=str))
    first = parts['name'].split()[0] if parts['name'] else ''
    how = 'template: no key'
    for spec in [cfg.get('dana', {}).get('draft_model'), *cfg['stack']['brain']]:
        model = brain.link(spec) if spec else None
        if model is None:
            continue

        async def run(model=model) -> str:
            out = ''
            async with model.chat(chat_ctx=ctx) as stream:
                async for chunk in stream:
                    if chunk.delta and chunk.delta.content:
                        out += chunk.delta.content
            return out.strip()
        try:
            text = await asyncio.wait_for(run(), 12)
        except Exception as e:  # this link failed: the next one, then the plain join
            how = f"template: {spec['model']} {type(e).__name__}"
            continue
        text = (f'Hi {first}. ' if first else '') + text
        problems = check(text, parts) if text.strip() else ['empty']
        if not problems:
            return text, f"riley ({spec['model']})"
        return template(parts), f'template: fact check refused {problems}'
    return template(parts), how


def subject(message: dict, application: dict) -> str:
    role = application.get('title') or (message.get('parts') or {}).get('role') or 'your application'
    kind = message.get('kind')
    if kind == 'next_round':
        return f'Next step for the {role} role: pick a second interview time'
    if kind == 'not_yet':
        return f'Your application for {role}: not yet, and how to come back'
    offers = (message.get('parts') or {}).get('offers')
    return f'Your application for {role}' + (', and another role that fits' if offers else '')


def recruiter_of(by: str, said: dict) -> str:
    """The recruiter a real org's message names: the person who decided, by name; an address or Riley's own automatic
    decision gives the org's recruiter instead (riley/tenant.py names)."""
    by = (by or '').strip()
    return said['recruiter'] if not by or '@' in by or by == AUTO_BY else by[:80]


def email(message: dict, application: dict) -> dict:
    """Dana's email for one message: {subject, text, html}, signed by her. No provider yet: it is shown and saved as
    an .eml (`eml()`), and `messages.channel` says so."""
    parts = _load(message.get('parts')) or {}
    employer = parts.get('employer', 'Hudson Park Health')
    recruiter = parts.get('recruiter', 'Dana')
    # Dana's own edit goes out as she wrote it; otherwise the same parts in her first person
    edited = _load(message.get('edits'))
    body = (message.get('text') or '') if edited or not parts.get('result') else template(parts, first_person=True)
    # a real org's own signature when it set one (Settings), else the recruiter's name and the employer
    sign = parts.get('signature') or f'{recruiter}\nRecruiter, {employer}'
    reply = ('Reply to this email or call our recruitment line, and you will reach me or Riley, our AI assistant.')
    fair = fair_link(message, application)
    note = f'Optional: help us check our process is fair: {fair}' if fair else ''
    # A plain link to sign in by email, not a one-time token: this email may be read days later (decision 0022)
    portal = f"See where your applications stand any time: {portal_link()}"
    text = f'{body}\n\n{reply}\n\n{portal}\n\n' + (f'{note}\n\n' if note else '') + f'{sign}\n'
    paras = ''.join(f'<p>{html.escape(p)}</p>' for p in (body, reply))
    paras += (f'<p>See where your applications stand any time: <a href="{html.escape(portal_link())}">'
              'sign in with this email address</a></p>')
    if fair:  # one quiet line, the same as on the /try page
        paras += (f'<p style="font-size:13px;color:#666">Optional: <a href="{html.escape(fair)}" style="color:#666">'
                  'help us check our process is fair</a></p>')
    page = (f'<div style="font-family:Georgia,serif;font-size:16px;line-height:1.5;color:#222;max-width:560px">'
            f'{paras}<p>{"<br>".join(html.escape(line) for line in sign.splitlines())}</p></div>')
    return {'subject': subject({**message, 'parts': parts}, application), 'text': text, 'html': page}


def portal_link() -> str:
    """The candidate sign-in page (web/auth.py): they get a link to the address on their application."""
    return os.environ.get('PUBLIC_URL', '').rstrip('/') + '/console/login?as=candidate'


def fair_link(message: dict, application: dict) -> str | None:
    """The bias check's optional form for this person (riley/bias.py token), as on the /try page. A full address
    when PUBLIC_URL is set, so it works from a mail client; else the path."""
    who, sandbox = application.get('candidate_id'), message.get('sandbox')
    if who is None or not sandbox:
        return None
    base = os.environ.get('PUBLIC_URL', '').rstrip('/')
    return f'{base}/self-id?token={bias.token(sandbox, int(who))}'


def eml(message: dict, application: dict, to: str | None = None) -> bytes:
    """The email as an .eml file to download, until an email provider is set up."""
    e = email(message, application)
    m = EmailMessage()
    m['Subject'] = e['subject']
    parts = _load(message.get('parts')) or {}
    if parts.get('recruiter') and parts.get('employer') and parts.get('employer') != 'Hudson Park Health':
        # a real org's sender: its recruiter and name, on Riley's own address (no org has its own mail yet)
        sender = re.sub(r'[<>"\r\n]', '', f"{parts['recruiter']} at {parts['employer']}")[:120]
        m['From'] = f'{sender} <{FROM_ADDRESS}>'
    else:
        m['From'] = 'Dana at Hudson Park Health <dana@hudsonpark.example>'
    m['To'] = to or application.get('email') or 'candidate@example.com'
    m.set_content(e['text'])
    m.add_alternative(e['html'], subtype='html')
    return bytes(m)


def short_items(criteria: list[dict], facts: dict, job: dict, scorecard: dict | None) -> list[dict]:
    """The required items a screen fell short on, for `auto()`: from matching when it is there, else from the
    screen's knockout flags."""
    required = [c for c in criteria if c.get('kind') == 'required']
    match = track('match')  # until track 5 merges, the scorecard's flags decide
    if match and criteria:
        fit = match.fit(facts or {}, job, criteria)
        names = set()
        for x in [*fit.get('missing', []), *fit.get('unknown', [])]:
            names |= {str(x.get('id')), str(x.get('name'))} if isinstance(x, dict) else {str(x)}
        return [c for c in required if str(c.get('id')) in names or str(c.get('name')) in names]
    flags = ' '.join((scorecard or {}).get('flags', [])).lower()
    # ' rn' once matched "1 year as an RN" too, so a missing license also read as missing years
    words = {'license': ('license',), 'certifications': ('bls', 'cert'), 'nights': ('night', 'shift'),
             'work_authorization': ('authoriz',)}
    flagged = [q for q in words if flags.startswith(q) or f' {q}:' in f' {flags}']
    out = [c for c in required
           if any(w.strip() in str(c.get('name', '')).lower() for q in flagged for w in words[q])]
    return out


def auto_result(short: list[dict], scorecard: dict) -> str:
    """Every required item met and the screen says advance: next round. One item short that Dana gave a way back
    for, or a screen left unfinished: not yet. Else not selected."""
    flagged = bool(scorecard.get('flags'))
    if not short and not flagged:
        return 'next_round' if scorecard.get('next_step', 'advance to interview') == 'advance to interview' \
            else 'not_yet'
    if len(short) == 1 and short[0].get('change'):
        return 'not_yet'
    return 'not_selected'


def held(result: str, job: dict) -> bool:
    """An automatic no waits for Dana when the job is in "Dana confirms every no" (`bias.needs_dana`). A yes never
    waits."""
    return result != 'next_round' and bias.needs_dana(job, result)


async def _confirms_every_no(sandbox: str, job: dict) -> bool:
    # Set on the job already, or by the bias check run now; a check that can't run holds the no for Dana
    if job.get('confirm_every_no'):
        return True
    try:
        return await bias.confirms_every_no(sandbox, job['id'])
    except Exception as e:
        print('bias check failed, holding the no for Dana', job['id'], type(e).__name__, e, flush=True)
        return True


def track(name: str):
    """Another track's module (match: track 5, outbound: track 9), or None until it has merged."""
    import importlib
    try:
        return importlib.import_module(f'.{name}', __package__)  # agent.match here, riley.match in take2-riley
    except ImportError:
        return None


def _load(v):
    return json.loads(v) if isinstance(v, str) else v


# ---- reads and writes ----

async def get_mode(sandbox: str) -> str:
    await store.ensure()
    rows = await octo.query("select value from settings where sandbox = $1 and key = 'outcome_mode'", [sandbox])
    mode = rows[0]['value'] if rows else None
    if isinstance(mode, str) and mode.startswith('"'):  # jsonb can come back as its JSON text
        mode = json.loads(mode)
    if mode in MODES:
        return mode
    return ORG_MODE if re.fullmatch(r'org_\d+', sandbox or '') else DEFAULT_MODE


async def set_mode(sandbox: str, mode: str, by: str) -> None:
    if mode not in MODES:
        raise ValueError(f'mode must be one of {MODES}')
    await store.ensure()
    await octo.query("insert into settings (sandbox, key, value, updated) values ($1, 'outcome_mode', $2, now()) "
                     'on conflict (sandbox, key) do update set value = excluded.value, updated = now()',
                     [sandbox, json.dumps(mode)])
    await store.log(sandbox, 'mode', detail={'outcome_mode': mode, 'by': by})


async def application(application_id: int) -> dict:
    rows = await octo.query(
        'select a.*, c.name, c.email, c.phone, c.timezone, c.language, c.facts as candidate_facts, '
        "(to_jsonb(c) ->> 'guest') as guest, j.title, j.location, j.slots, j.options, j.confirm_every_no, "
        'h.name as hospital '
        'from applications a join candidates c on c.id = a.candidate_id join jobs j on j.id = a.job_id '
        'left join hospitals h on h.id = j.hospital_id '
        'where a.id = $1', [application_id])
    if not rows:
        raise ValueError(f'no application {application_id}')
    return rows[0]


async def message(message_id: int) -> dict:
    rows = await octo.query('select * from messages where id = $1', [message_id])
    if not rows:
        raise ValueError(f'no message {message_id}')
    return rows[0]


async def decide(sandbox: str, application_id: int, result: str, reason: str, criteria_ids: list[int], by: str,
                 offers: list[dict]) -> dict:
    """Dana's decision: written to `applications.decision`, and a draft message from the records. Nothing is sent."""
    await store.ensure()
    cfg = config()
    app = await application(application_id)
    sandbox = app.get('sandbox') or sandbox  # the message and its log go where the application is
    crit = await octo.query('select * from criteria where job_id = $1 and id in '
                            '(select jsonb_array_elements_text($2::jsonb)::bigint) order by id',
                            [app['job_id'], json.dumps(list(criteria_ids or []))]) if criteria_ids else []
    now = datetime.now(UTC)
    tz = app.get('timezone') or 'America/New_York'
    job = {'title': app['title'], 'location': app['location'], 'hospital': app.get('hospital'),
           'slots': _load(app.get('slots')), 'options': _load(app.get('options'))}
    resumes = await octo.query('select facts from resumes where candidate_id = $1 order by at desc limit 1',
                               [app['candidate_id']])
    said = await tenant.names_for(sandbox, cfg)
    parts = parts_for(result, reason, crit, offers, job, app.get('name') or '', tenant.branded(cfg, said), now, tz,
                      answers=_load(app.get('answers')) or {}, resume=_load(resumes[0]['facts']) if resumes else {})
    if said['org']:  # a real org's message is from the person who decided, signed as the org says (riley/tenant.py)
        parts['recruiter'] = recruiter_of(by, said)
        if said['signature']:
            parts['signature'] = said['signature']
    text, how = await draft(parts, cfg)
    parts['drafted_by'] = how
    decision = {'result': result, 'reason': reason, 'criteria': list(criteria_ids or []), 'by': by,
                'at': now.isoformat()}
    await octo.query('update applications set decision = $2, updated = now() where id = $1',
                     [application_id, json.dumps(decision)])
    rows = await octo.query(
        "insert into messages (sandbox, application_id, kind, status, parts, text, channel) "
        "values ($1, $2, $3, 'draft', $4, $5, $6) returning *",
        [sandbox, application_id, result, json.dumps(parts, default=str), text,
         'call; email shown and downloadable (no email provider yet)'])
    await store.log(sandbox, 'decision', app['candidate_id'], application_id, {**decision, 'message_id': rows[0]['id']})
    return rows[0]


async def approve(message_id: int, by: str, text: str | None = None, ring: bool = True) -> dict:
    """Dana approves a draft, with her edit if she made one (kept in `edits`). In 'immediate' and 'dana_now' the
    call rings now; in 'normal' it waits for their next call, or the scheduler outside quiet hours. `ring` False (the
    demo, for a number it may not ring: web/tracker/rules.py may_ring) approves without ringing."""
    m = await message(message_id)
    if m['status'] not in ('draft', 'approved'):
        raise ValueError(f"message {message_id} is {m['status']}")
    edits = _load(m.get('edits')) or []
    if text and text.strip() != m['text']:
        edits.append({'by': by, 'at': datetime.now(UTC).isoformat(), 'text': text.strip(), 'was': m['text']})
    rows = await octo.query(
        "update messages set status = 'approved', approved_by = $2, approved = now(), text = $3, edits = $4 "
        'where id = $1 returning *', [message_id, by, (text or m['text']).strip(), json.dumps(edits)])
    m = rows[0]
    app = await application(m['application_id'])
    await store.log(m['sandbox'], 'approved', app['candidate_id'], m['application_id'],
                    {'message_id': message_id, 'by': by, 'edited': bool(text and edits)})
    mode = await get_mode(m['sandbox'])
    later = ring_after(m)
    if ring and rings_now(mode, m) and not later:
        return {**m, 'ring': await _ring(m, app, by)}
    due = later or next_allowed(datetime.now(UTC), app.get('timezone') or 'America/New_York')
    await store.log(m['sandbox'], 'outcome_queued', app['candidate_id'], m['application_id'],
                    {'message_id': message_id, 'mode': mode}, due=due)
    return {**m, 'ring': {'placed': False, 'why': f'{mode} mode: given on their next call, or after {due.isoformat()}'}}


async def call_now(message_id: int, by: str) -> dict:
    """The tracker's "Call back now": ring for one approved message, whatever the mode."""
    m = await message(message_id)
    if not can_deliver(m):
        return {'placed': False, 'why': f"message is {m['status']}, not approved and waiting"}
    if await rung(message_id):  # approving in 'immediate' or 'dana_now' rang already: never a second call
        return {'placed': False, 'why': 'Riley already rang for this message'}
    return await _ring(m, await application(m['application_id']), by)


async def rung(message_id: int) -> bool:
    """Whether a ring was placed for this message already (a `ring` event that says placed)."""
    rows = await octo.query("select 1 from events where kind = 'ring' and detail->>'message_id' = $1 "
                            "and (detail->>'placed')::boolean is true limit 1", [str(int(message_id))])
    return bool(rows)


async def _ring(m: dict, app: dict, by: str) -> dict:
    """Ring the candidate for an approved message through track 9's outbound.place(), and log it either way."""
    # Octo returns bigint as text; the room metadata carries numbers
    meta = {'mode': 'outcome', 'candidate_id': int(app['candidate_id']), 'application_id': int(m['application_id']),
            'message_id': int(m['id'])}
    if not can_deliver(m):
        result = {'placed': False, 'why': 'not approved'}
    elif not app.get('phone'):
        result = {'placed': False, 'why': 'no phone on file'}
    else:
        outbound = track('outbound')
        try:
            result = (await outbound.place(app['phone'], meta, sandbox=m['sandbox'],
                                           consented=outbound.consented(app, app['phone'])) if outbound else
                      {'placed': False, 'why': 'no outbound calling yet: given on their next call'})
        except Exception as e:
            result = {'placed': False, 'why': f'{type(e).__name__}: {e}'}
    await store.log(m['sandbox'], 'ring', app['candidate_id'], m['application_id'], {**meta, **result, 'by': by})
    return result


async def auto(sandbox: str, application_id: int) -> dict | None:
    """The demo flow ('immediate' mode only): decide from the screen at once with Dana's pre-written reasons,
    approve as 'dana (auto, demo)' and ring. None in any other mode, or if a decision is already made."""
    app = await application(application_id)
    sandbox = app.get('sandbox') or sandbox  # a /try guest's application is in the try sandbox
    if not auto_decides(await get_mode(sandbox)):
        return None
    if _load(app.get('decision')):
        return None
    cfg = config()
    crit = await octo.query('select * from criteria where job_id = $1', [app['job_id']])
    job = {'title': app['title'], 'slots': _load(app.get('slots')), 'id': app['job_id'],
           'confirm_every_no': bool(app.get('confirm_every_no'))}
    score = _load(app.get('score')) or {}
    short = short_items(crit, _load(app.get('candidate_facts')) or {}, job, score)
    result = auto_result(short, score)
    # Dana's pre-written words: each short item's reason, or her line for an unfinished or flagged screen
    reasons = cfg.get('dana', {}).get('reasons', {})
    # (each short item's reason and change are already in the parts)
    reason = '' if short or result == 'next_round' else reasons.get('flagged' if score.get('flags') else 'incomplete',
                                                                      '')
    offers: list[dict] = []
    match = track('match')
    if result != 'next_round' and match:
        offers = await match.offers(sandbox, app['candidate_id'], exclude_job_id=app['job_id'])
    m = await decide(sandbox, application_id, result, reason, [c['id'] for c in short], AUTO_BY, offers)
    # Track 12: on a job flagged for possible adverse impact Dana confirms every no, so it waits as a draft for her
    if result != 'next_round':
        job['confirm_every_no'] = await _confirms_every_no(sandbox, job)
    if held(result, job):
        await store.log(sandbox, 'auto_held', app['candidate_id'], application_id,
                        {'message_id': int(m['id']), 'result': result,
                         'why': 'bias check: this job needs Dana for every no; left as a draft for her'})
        return m
    wait = cfg.get('dana', {}).get('ring_after_minutes') or 0
    parts = {**(_load(m['parts']) or {}), 'send_without_asking': True,
             **({'ring_after': (datetime.now(UTC) + timedelta(minutes=wait)).isoformat()} if wait else {})}
    await octo.query('update messages set parts = $2 where id = $1', [m['id'], json.dumps(parts, default=str)])
    return await approve(m['id'], AUTO_BY)


async def other_roles(sandbox: str, candidate_id: int, exclude_job_id: int | None) -> list[dict]:
    """On a call, when they ask about other roles: the open jobs whose every required item they meet, as offers."""
    match = track('match')
    return await match.offers(sandbox, candidate_id, exclude_job_id, asked=True) if match else []


async def pending(sandbox: str, candidate_id: int) -> dict | None:
    """The approved, undelivered message for this caller, newest first, with its application's role."""
    rows = await octo.query(
        'select m.*, a.candidate_id, a.job_id, j.title from messages m join applications a on a.id = m.application_id '
        "join jobs j on j.id = a.job_id where m.sandbox = $1 and a.candidate_id = $2 and m.status = 'approved' "
        'and m.delivered is null order by m.approved desc limit 5', [sandbox, candidate_id])
    return next((m for m in rows if not ring_after(m)), None)


async def deliver(message_id: int) -> bool:
    """The message was given on a call: delivered, and the application's status moves. Only an approved message;
    answers, score and decision are never touched."""
    rows = await octo.query("update messages set status = 'delivered', delivered = now() "
                            "where id = $1 and status = 'approved' and delivered is null returning *", [message_id])
    if not rows:
        return False
    m = rows[0]
    await octo.query('update applications set status = $2, updated = now() where id = $1',
                     [m['application_id'], STATUS_AFTER[m['kind']]])
    await store.log(m['sandbox'], 'delivered', None, m['application_id'], {'message_id': message_id,
                                                                           'channel': 'call'})
    return True


async def book(message_id: int, slot: str, with_whom: str) -> dict:
    """Book one of the message's listed slots, with the recruiter or a nurse interviewer: a `booked` event, and the
    time comes off the job's open slots. Anything not on the list, or already taken, is refused."""
    if with_whom not in WITH:
        return {'booked': False, 'why': f'with must be one of {sorted(WITH)}'}
    m = await message(message_id)
    if m['kind'] != 'next_round' or m['status'] not in ('approved', 'delivered'):
        return {'booked': False, 'why': 'not an approved next-round message'}
    chosen = pick_slot((_load(m['parts']) or {}).get('slots', []), slot, with_whom)
    if not chosen:
        return {'booked': False, 'why': 'not one of the listed times'}
    app = await application(m['application_id'])
    left = [s for s in _load(app.get('slots')) or []
            if store.ts(s.get('at') if isinstance(s, dict) else s) != store.ts(chosen['at'])]
    if len(left) == len(_load(app.get('slots')) or []):
        return {'booked': False, 'why': 'that time was just taken'}
    await octo.query('update jobs set slots = $2 where id = $1', [app['job_id'], json.dumps(left)])
    detail = {'message_id': int(message_id), 'at': chosen['at'], 'say': chosen['say'], 'with': with_whom}
    # The interviewer the slot was opened for, if any, so only they see it among their interviews (web/console.py mine)
    opened = next((s for s in _load(app.get('slots')) or [] if isinstance(s, dict)
                   and store.ts(s.get('at')) == store.ts(chosen['at'])), {})
    if (who := chosen.get('interviewer_id') or opened.get('interviewer_id')) is not None:
        detail['interviewer_id'] = who
    await store.log(m['sandbox'], 'booked', app['candidate_id'], m['application_id'], detail)
    from . import scheduler  # track 11: the reminder and reconfirm request before it
    await scheduler.queue_reminder(m['sandbox'], app['candidate_id'], m['application_id'], detail, app['title'])
    return {'booked': True, **detail}


# ---- Dana's recorded lines ----

_tasks: set = set()


def in_background(coro) -> None:
    """Run it without waiting, keeping a reference so it isn't collected half way."""
    import asyncio
    t = asyncio.ensure_future(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


def dana_lines(cfg: dict) -> dict:
    return cfg.get('dana', {}).get('lines', {})


def dana_voice(cfg: dict):
    """Dana's own voice (screen.yaml dana.voice), distinct from Riley's; None without a key."""
    v = cfg.get('dana', {}).get('voice') or {}
    if v.get('provider') != 'cartesia' or not os.environ.get('CARTESIA_API_KEY'):
        return None
    from livekit.plugins import cartesia
    base: dict[str, Any] = {'base_url': os.environ['CARTESIA_BASE_URL']} if os.environ.get('CARTESIA_BASE_URL') else {}
    return cartesia.TTS(model=v.get('model', 'sonic-3'), voice=v['id'], **base)


def _clip(text: str) -> Path:
    import hashlib

    from .pro_speech import KEPT
    return KEPT / ('dana-' + hashlib.sha256(text.encode()).hexdigest()[:16] + '.wav')


def clip_path(kind: str, cfg: dict | None = None) -> Path | None:
    """Dana's recorded line for this kind of outcome as a WAV file, if it has been made (for the web page)."""
    text = dana_lines(cfg or config()).get(kind)
    path = _clip(text) if text else None
    return path if path and path.exists() else None


def clip_frames(kind: str, cfg: dict | None = None) -> list | None:
    """The same clip as audio frames for the call."""
    import wave

    from livekit import rtc
    path = clip_path(kind, cfg)
    if not path:
        return None
    with wave.open(str(path)) as w:
        rate, pcm = w.getframerate(), w.readframes(w.getnframes())
    step = rate // 10 * 2  # 100 ms of 16-bit mono
    return [rtc.AudioFrame(pcm[i:i + step], rate, 1, len(pcm[i:i + step]) // 2) for i in range(0, len(pcm), step)]


async def keep_clips(cfg: dict | None = None) -> list[str]:
    """Record each of Dana's lines once in her voice and keep it on disk. Returns the kinds now kept."""
    import wave
    cfg = cfg or config()
    voice = dana_voice(cfg)
    kept = []
    for kind, text in dana_lines(cfg).items():
        path = _clip(text)
        if not path.exists() and voice is not None:
            try:
                frames = [ev.frame async for ev in voice.synthesize(text)]
                path.parent.mkdir(parents=True, exist_ok=True)
                with wave.open(str(path.with_suffix('.part')), 'wb') as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(frames[0].sample_rate)
                    w.writeframes(b''.join(bytes(f.data) for f in frames))
                path.with_suffix('.part').rename(path)
            except Exception as e:  # no clip: Riley gives the message without it
                print('dana clip failed', kind, type(e).__name__, e, flush=True)
        if path.exists():
            kept.append(kind)
    return kept


if __name__ == '__main__':
    import asyncio

    from . import keys
    keys.load()

    async def main():  # outside a worker, the plugin needs its own http session
        from livekit.agents.utils import http_context
        async with http_context.open():
            print('kept:', await keep_clips())
    asyncio.run(main())
