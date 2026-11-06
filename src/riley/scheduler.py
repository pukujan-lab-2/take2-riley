"""The scheduler: every call Riley owes someone, at the right time, to the right people (SPEC §6, BUILD-PLAN track 11).

What comes due (events with `due` set and `done` empty, plus applications whose promised date is close):
  callback_due        the time they chose on a screen (`schedule_callback`): Riley rings to finish it
  outcome_queued      an approved message in 'normal' mode (riley/outcome.py approve): the outcome call
  reminder            before a booked second interview: a reminder and a request to reconfirm
  no_show_follow_up   after a missed interview (`no_show`): told plainly, one more chance
  date update         a promised date that is about to pass with no decision: the new date and the reason, first

Limits, checked in code before every call: 8 am to 9 pm in their time zone (immediate, the demo mode, skips it, as
the Outcome modes table says); only people who consented; one person's updates due the same day go out in one call.
Made-up people (a visitor's guest cast, the bias seed) and 555-01xx numbers are never called.
Calls go through riley/outbound.py `place`; a callback writes `callback {answered}`, which riley/signals.py reads.
Texts wait for carrier registration.

The rules are plain functions, so tests check them without a database or a phone. The loop runs in the web app, one
process, and only where SCHEDULER=on (the VPS web container), so a checkout on a laptop never rings the trunk.
"""
import asyncio
import json
import os
import re
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from . import octo, outcome, store
from .helpline import when

TZ = 'America/New_York'
KINDS = ('callback_due', 'outcome_queued', 'reminder', 'no_show_follow_up')
UPDATES = ('outcome', 'reminder', 'no_show_follow_up', 'date_update')   # combined into one call per person per day
EVERY = 60                              # seconds between runs
LEAD = timedelta(hours=14)              # a date update is due this long before the promise; covers the longest night
REMIND = timedelta(hours=24)            # a reminder this long before the interview, or 2 hours if booked later
GAP = timedelta(minutes=30)             # no second call to one person this soon after the last
DEFAULT_HOUR = time(10)                 # (default) a day with no time, or words with neither: 10 am their time
OLDEST = timedelta(days=7)              # a promise that passed longer ago than this is Dana's, not a call

WORDS = {w: i for i, w in enumerate(['zero', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine',
                                     'ten', 'eleven', 'twelve'])}
PARTS_OF_DAY = {'morning': 10, 'noon': 12, 'lunch': 12, 'midday': 12, 'afternoon': 14, 'evening': 18, 'tonight': 19,
                'night': 19}
CLOCK = re.compile(r'\b(\d{1,2}|' + '|'.join(WORDS) + r')(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?|o\'?clock)?(?![\d:])')
IN = re.compile(r'\bin (?:(an?|half an|\d+|' + '|'.join(WORDS) + r') )?(hour|hours|minute|minutes|min)\b')


# ---- rules, without a database ----

def local(dt: datetime, tz: str) -> datetime:
    return dt.astimezone(ZoneInfo(tz))


def at_local(d: date, t: time, tz: str) -> datetime:
    return datetime.combine(d, t, tzinfo=ZoneInfo(tz))


def end_of_day(now: datetime, tz: str) -> datetime:
    """The last moment of their day: what 'the same day' means when updates are combined."""
    return at_local(local(now, tz).date() + timedelta(days=1), time(0), tz) - timedelta(microseconds=1)


def _hour(text: str) -> tuple[int, int] | None:
    """The clock time in what they said, as (hour, minute) on a 24-hour clock. A bare 1 to 7 is afternoon (nobody
    asks for a call at 5 am), 8 to 11 is morning."""
    for m in CLOCK.finditer(text):
        raw, minute, suffix = m.group(1), int(m.group(2) or 0), (m.group(3) or '').replace('.', '')
        h = int(raw) if raw.isdigit() else WORDS[raw]
        # A bare number is a time only after a word that says so ("at 5", "after five"), or with am/pm
        before = text[:m.start()].rstrip()
        if not suffix and not re.search(r'\b(at|after|around|about|by|from|before|till|until|like)$', before):
            continue
        if h > 23 or minute > 59 or (suffix in ('am', 'pm') and not 1 <= h <= 12):
            continue
        if suffix == 'pm' and h < 12:
            h += 12
        elif suffix == 'am' and h == 12:
            h = 0
        elif suffix not in ('am', 'pm') and 1 <= h <= 7:
            h += 12
        return h, minute
    for word, h in PARTS_OF_DAY.items():
        if re.search(rf'\b{word}\b', text):
            return h, 0
    return None


def parse_when(said: str, now: datetime, tz: str = TZ) -> datetime | None:
    """The moment their words name ("tomorrow after 5", "Monday morning", "in two hours"), in their time zone; None
    when the words name no day or time, or one already past."""
    text = (said or '').lower().strip()
    if not text:
        return None
    m = IN.search(text)
    if m:
        n = m.group(1)
        count = 0.5 if n == 'half an' else 1 if n in (None, 'a', 'an') else int(n) if n.isdigit() else WORDS[n]
        unit = timedelta(hours=1) if m.group(2).startswith('hour') else timedelta(minutes=1)
        return now + count * unit if count else None
    today = local(now, tz).date()
    day = None
    if re.search(r'\b(today|tonight|this (morning|afternoon|evening))\b', text):
        day = today
    elif re.search(r'\btomorrow\b', text):
        day = today + timedelta(days=1)
    else:
        for i, name in enumerate(outcome.WEEKDAYS):
            if re.search(rf'\b{name}\b', text):
                day = today + timedelta(days=(i - today.weekday()) % 7)   # today's name: today, if still ahead
                break
    hm = _hour(text)
    if day is None and hm is None:
        return None
    t = time(*hm) if hm else DEFAULT_HOUR
    if day is None:  # a time with no day: today if it is still ahead, else tomorrow
        day = today if at_local(today, t, tz) > now else today + timedelta(days=1)
    out = at_local(day, t, tz)
    if out <= now and day == today and not re.search(r'\b(today|tonight|this)\b', text):
        out = at_local(day + timedelta(days=7), t, tz)   # "Friday at 9", said on Friday at 10: next Friday
    return out if out > now else None


def callback_time(said: str, now: datetime, tz: str = TZ) -> datetime:
    """When to ring for a callback: the time they said, or (default) the next morning at 10 their time."""
    return parse_when(said, now, tz) or at_local(local(now, tz).date() + timedelta(days=1), DEFAULT_HOUR, tz)


def reminder_due(at: datetime, now: datetime) -> datetime | None:
    """A day before the interview; 2 hours before when it was booked closer than that; none when even that is past."""
    for before in (REMIND, timedelta(hours=2)):
        if at - before > now:
            return at - before
    return None


def update_due(promised_by: datetime) -> datetime:
    """When the date update for a promise comes due. Fourteen hours is longer than any night's quiet hours (twelve on
    the night the clocks go back), so the first allowed moment after it is always before the promise."""
    return promised_by - LEAD


def date_update(app: dict, now: datetime, recruiter: str) -> dict:
    """The new date and the reason, from the records, for a promise about to pass with no decision out."""
    old = store.ts(app['promised_by'])
    assert old is not None
    to = max(old, now) + timedelta(hours=store.REPLY_HOURS)
    closes = store.window_closes({'first_applied': store.ts(app.get('first_applied')),
                                  'window_hours': app.get('window_hours') or store.WINDOW_HOURS})
    if app.get('status') == 'applied':
        why = "your phone screen isn't finished yet, and you can finish it any time on this number"
    elif closes and closes > old:
        why = 'the role holds every decision until its review window closes, so everyone who applies gets a fair look'
    else:
        why = f'{recruiter} needs more time to read every screen for this role'
    return {'from': old.isoformat(), 'to': to.isoformat(), 'reason': why}


def consented(person: dict, kind: str) -> tuple[bool, str]:
    """Only people who consented (SPEC §6). A "no" on file wins over everything; a "yes", or a guest's tick on /try
    while their number is kept, allows every call; with nothing on file, only the callback they asked for. A made-up
    person (the guest cast or the bias seed, `facts.demo`) is never called, whatever is on file."""
    if person.get('demo'):
        return False, 'made-up demo person: never called'
    calls = (store._j_load(person.get('consent')) or {}).get('calls')
    if calls is False:
        return False, 'said no to calls'
    if calls is True:
        return True, 'consented to calls'
    if person.get('guest'):
        return True, 'ticked "Riley may call this number" on /try'
    if kind == 'callback':
        return True, 'asked for this callback'
    return False, 'no consent to calls on file: told on their next call instead'


def may_call_now(now: datetime, tz: str, mode: str) -> bool:
    """8 am to 9 pm their time. The demo mode (immediate) skips quiet hours, as in the Outcome modes table."""
    return mode == 'immediate' or not outcome.quiet(now, tz)


def kind_of(item: dict) -> str:
    return str({'callback_due': 'callback', 'outcome_queued': 'outcome'}.get(item['kind'], item['kind']))


def _at(v) -> datetime:
    t = store.ts(v)
    assert isinstance(t, datetime), v
    return t


def plan(items: list[dict], people: dict, modes: dict, now: datetime, last_call: dict | None = None) -> dict:
    """What to do now with each item: {'calls': [...], 'skip': [(item, why)], 'wait': [item]}.

    Every item due by now is in exactly one of them; an update due later today rides along with a call made for
    one due now. A call is {candidate_id, sandbox, phone, mode, application_id, message_id, items}."""
    last_call = last_call or {}
    calls, skip, wait = [], [], []
    by: dict[int, list[dict]] = {}
    for it in items:
        by.setdefault(int(it['candidate_id']), []).append(it)
    for cid, mine in sorted(by.items()):
        person = people.get(cid) or {}
        tz = person.get('timezone') or TZ
        due_now = [i for i in mine if store.ts(i['due']) <= now]
        if not due_now:
            continue
        keep = []
        for it in mine:
            ok, why = consented(person, kind_of(it))
            if not person.get('phone'):
                ok, why = False, 'no phone on file'
            elif store.fictional(person['phone']):
                ok, why = False, 'a made-up 555-01xx number'
            if ok and kind_of(it) == 'outcome' and not outcome.can_deliver(it.get('message') or {}):
                ok, why = False, 'message already given, or not approved'
            if ok:
                keep.append(it)
            elif it in due_now:
                skip.append((it, why))
        due_now = [i for i in keep if store.ts(i['due']) <= now]
        last = last_call.get(cid)
        if not due_now:
            continue
        mode = modes.get(person.get('sandbox') or due_now[0].get('sandbox'), outcome.DEFAULT_MODE)
        # In the demo mode an outcome skips the gap: a retaken screen's outcome follows the last one within minutes
        rushed = mode == 'immediate' and any(kind_of(i) == 'outcome' for i in due_now)
        if not may_call_now(now, tz, mode) or (last and now - last < GAP and not rushed):
            wait += due_now
            continue
        base = {'candidate_id': cid, 'sandbox': person.get('sandbox') or due_now[0].get('sandbox'),
                'phone': person['phone'], 'message_id': None}
        callbacks = [i for i in due_now if kind_of(i) == 'callback']
        if callbacks:  # the screen they asked to finish: its own call; their updates wait for the next run
            calls.append({**base, 'mode': 'callback', 'application_id': callbacks[0].get('application_id'),
                          'items': callbacks})
            wait += [i for i in due_now if kind_of(i) != 'callback']
            continue
        today = end_of_day(now, tz)
        group = [i for i in keep if kind_of(i) in UPDATES and store.ts(i['due']) <= today]
        outcomes = [i for i in group if kind_of(i) == 'outcome']
        # One outcome message per call; another one due the same day waits for the next call
        wait += [i for i in outcomes[1:] if i in due_now]
        group = [i for i in group if i not in outcomes[1:]]
        lead = outcomes[0] if outcomes else group[0]
        calls.append({**base, 'mode': 'outcome' if outcomes else 'follow_up',
                      'application_id': lead.get('application_id'),
                      'message_id': lead.get('message_id') if outcomes else None, 'items': group})
    return {'calls': calls, 'skip': skip, 'wait': wait}


def metadata(call: dict) -> dict:
    """The room metadata for a call: the mode and ids only (riley/outbound.py keeps nothing else)."""
    meta = {'mode': call['mode'], 'candidate_id': int(call['candidate_id'])}
    if call.get('application_id') is not None:
        meta['application_id'] = int(call['application_id'])
    if call.get('message_id') is not None:
        meta['message_id'] = int(call['message_id'])
    ids = [int(i['event_id']) for i in call['items'] if i.get('event_id') is not None and kind_of(i) != 'outcome']
    if ids:
        meta['event_ids'] = ids
    return meta


def after_dial(call: dict, result: dict) -> list[tuple]:
    """The writes once a call was tried: each item done with what happened, one `scheduled_call` event, and for a
    callback `callback {answered}` when the line said whether they picked up. A failed dial is no signal."""
    answered = result.get('answered')
    note = {'placed': bool(result.get('placed')), 'why': result.get('why'), 'answered': answered}
    writes: list[tuple] = [('done', int(i['event_id']), note) for i in call['items'] if i.get('event_id') is not None]
    writes.append(('log', call['sandbox'], 'scheduled_call', call['candidate_id'], call.get('application_id'),
                   {**metadata(call), **note}))
    if call['mode'] == 'callback' and answered is not None:
        for i in call['items']:
            writes.append(('log', call['sandbox'], 'callback', call['candidate_id'], i.get('application_id'),
                           {'answered': bool(answered), 'asked_for': (i.get('detail') or {}).get('said')}))
    return writes


def lines(items: list[dict], recruiter: str, tz: str = TZ) -> list[str]:
    """What Riley says for each update, from its record only."""
    out = []
    for it in items:
        d = store._j_load(it.get('detail')) or {}
        role = d.get('title') or 'your'
        role = f'the {role} role' if d.get('title') else 'your application'
        k = kind_of(it)
        if k == 'reminder':
            who = outcome.WITH.get(d.get('with') or '', 'the hiring team')
            # One sentence that asks: Riley's speech filter keeps a question even late in a long reply
            out.append(f"A reminder: your second interview for {role} is {outcome.say_time(_at(d['at']), tz)}, "
                       f'with {who}; can you still make it?')
        elif k == 'no_show_follow_up':
            missed = f"We missed you at your second interview for {role} on {outcome.say_time(_at(d['at']), tz)}."
            out.append(missed + (f' You have one more chance: would you like {recruiter} to set a new time?'
                                 if not d.get('paused') else
                                 f' After two missed interviews the application is paused; {recruiter} can look at it '
                                 'again if you ask.'))
        elif k in ('date_update', 'date_moved'):
            out.append(f"An update on {role}: your answer is now due {when(store.ts(d['to']), tz)}, because "
                       f"{d['reason']}.")
    return out


# ---- reads and writes ----

def _ids(rows: list[dict], key: str) -> str:
    return json.dumps(sorted({int(r[key]) for r in rows if r.get(key) is not None}))


async def queue_callback(sandbox: str, candidate_id: int, application_id: int | None, said: str,
                         tz: str | None = None, now: datetime | None = None) -> datetime:
    """The callback a caller asked for on a screen, as scheduled work at the time they said."""
    now = now or datetime.now(UTC)
    due = callback_time(said, now, tz or TZ)
    await store.log(sandbox, 'callback_due', candidate_id, application_id,
                    {'said': said, 'parsed': parse_when(said, now, tz or TZ) is not None}, due=due)
    return due


async def queue_reminder(sandbox: str, candidate_id: int, application_id: int, booking: dict, title: str,
                         now: datetime | None = None) -> datetime | None:
    """A reminder and reconfirm request before a booked second interview (from outcome.book)."""
    at = store.ts(booking['at'])
    due = reminder_due(at, now or datetime.now(UTC)) if at else None
    if due:
        await store.log(sandbox, 'reminder', candidate_id, application_id,
                        {'at': booking['at'], 'with': booking.get('with'), 'message_id': booking.get('message_id'),
                         'title': title}, due=due)
    return due


async def no_show(sandbox: str, application_id: int) -> dict:
    """They missed the booked interview: a `no_show` event (riley/signals.py) and the follow-up call, told plainly
    with the chance they have left."""
    from . import signals
    app = await outcome.application(application_id)
    events = await octo.query('select kind, detail from events where application_id = $1 order by at',
                              [application_id])
    booked = next((store._j_load(e['detail']) for e in reversed(events) if e['kind'] == 'booked'), None)
    if not booked:
        return {'ok': False, 'why': 'no booked interview on this application'}
    await store.log(sandbox, 'no_show', app['candidate_id'], application_id, {'at': booked['at']})
    s = signals.signals([*events, {'kind': 'no_show'}], app)
    await store.log(sandbox, 'no_show_follow_up', app['candidate_id'], application_id,
                    {'at': booked['at'], 'title': app['title'], 'chances_left': s['chances_left'],
                     'paused': s['paused']}, due=datetime.now(UTC))
    return {'ok': True, **{k: s[k] for k in ('chances_left', 'paused')}}


async def load(now: datetime) -> tuple[list[dict], dict, dict, dict]:
    """(items, people, modes, last_call): what is due through the end of anyone's day, and who it is for."""
    kinds = ', '.join(f"'{k}'" for k in KINDS)   # constants, never input
    events = await octo.query(
        f'select id as event_id, sandbox, kind, candidate_id, application_id, detail, due from events '
        f'where done is null and due is not null and due <= $1 and kind in ({kinds}) and candidate_id is not null',
        [now + timedelta(days=1)])
    for e in events:
        e['detail'] = store._j_load(e['detail']) or {}
        if e['kind'] == 'outcome_queued':
            e['message_id'] = e['detail'].get('message_id')
    msgs = await octo.query('select id, status, delivered from messages where id in '
                            '(select jsonb_array_elements_text($1::jsonb)::bigint)', [_ids(events, 'message_id')])
    by_id = {int(m['id']): m for m in msgs}
    for e in events:
        if e.get('message_id') is not None:
            e['message'] = by_id.get(int(e['message_id']), {})
    # Promises about to pass with nothing approved or given: a date update, made up here and written when it goes out
    apps = await octo.query(
        'select a.id as application_id, a.sandbox, a.candidate_id, a.status, a.promised_by, j.title, '
        'j.first_applied, j.window_hours from applications a join jobs j on j.id = a.job_id '
        "where a.withdrawn is null and a.status in ('applied', 'screened') and a.promised_by <= $1 "
        'and a.promised_by > $2 and not exists (select 1 from messages m where m.application_id = a.id '
        "and m.status in ('approved', 'delivered'))", [now + LEAD, now - OLDEST])
    updates = [{'kind': 'date_update', 'event_id': None, 'sandbox': a['sandbox'], 'candidate_id': a['candidate_id'],
                'application_id': a['application_id'], 'due': update_due(_at(a['promised_by'])), 'app': a,
                'detail': {'title': a['title']}} for a in apps]
    items = events + updates
    people_rows = await octo.query(
        "select id, sandbox, phone, timezone, consent, (to_jsonb(c) ->> 'guest') as guest, facts ->> 'demo' as demo "
        'from candidates c '
        'where id in (select jsonb_array_elements_text($1::jsonb)::bigint)', [_ids(items, 'candidate_id')])
    people = {int(p['id']): p for p in people_rows}
    modes = {s: await outcome.get_mode(s) for s in {p['sandbox'] for p in people_rows}}
    recent = await octo.query(
        "select candidate_id, max(at) as at from events where kind in ('scheduled_call', 'ring') and at > $1 "
        'and candidate_id is not null group by candidate_id', [now - GAP])
    last_call = {int(r['candidate_id']): store.ts(r['at']) for r in recent}
    return items, people, modes, last_call


async def _write(writes: list[tuple]) -> None:
    for w in writes:
        if w[0] == 'done':
            await octo.query('update events set done = now(), detail = detail || $2 where id = $1',
                             [w[1], json.dumps(w[2], default=str)])
        else:
            _, sandbox, kind, cid, aid, detail = w
            await store.log(sandbox, kind, cid, aid, detail)


async def _move_date(item: dict, now: datetime, recruiter: str) -> None:
    """The new date goes on the application and in a `date_moved` event before the call that says it."""
    a = item['app']
    d = {**date_update(a, now, recruiter), 'title': a['title']}
    await octo.query('update applications set promised_by = $2, updated = now() where id = $1',
                     [a['application_id'], store.ts(d['to'])])
    rows = await octo.query(
        "insert into events (sandbox, candidate_id, application_id, kind, detail, due) values "
        "($1, $2, $3, 'date_moved', $4, $5) returning id",
        [a['sandbox'], a['candidate_id'], a['application_id'], json.dumps(d), now])
    item.update(event_id=rows[0]['id'], detail=d)


async def run_once(now: datetime | None = None, dial=None) -> dict:
    """One pass: load, plan, call, write. `dial` stands in for outbound.place in tests. Returns what it did."""
    now = now or datetime.now(UTC)
    if dial is None:
        if not os.environ.get('SIP_OUTBOUND_TRUNK_ID'):
            return {'calls': 0, 'why': 'no outbound trunk: everything waits for their next call'}
        from . import outbound
        dial = outbound.place
    await store.ensure()
    items, people, modes, last_call = await load(now)
    todo = plan(items, people, modes, now, last_call)
    for it, why in todo['skip']:
        if it.get('event_id') is not None:
            await _write([('done', int(it['event_id']), {'skipped': why})])
    recruiter = outcome.config()['agent']['recruiter']
    for call in todo['calls']:
        for it in call['items']:
            if it['kind'] == 'date_update':
                await _move_date(it, now, recruiter)
        try:
            result = await dial(call['phone'], metadata(call), wait=True)
        except Exception as e:  # one failed call never stops the others
            result = {'placed': False, 'why': f'{type(e).__name__}: {e}'}
        await _write(after_dial(call, result))
        print('scheduler call', call['mode'], call['candidate_id'], [i['kind'] for i in call['items']],
              result.get('placed'), result.get('why'), flush=True)
    return {'calls': len(todo['calls']), 'skipped': len(todo['skip']), 'waiting': len(todo['wait'])}


async def follow_up_items(event_ids: list[int]) -> list[dict]:
    """The updates a follow-up call (or an outcome call's extras) says, by the event ids in its room metadata."""
    if not event_ids:
        return []
    rows = await octo.query('select id as event_id, kind, candidate_id, application_id, detail from events where id in '
                            '(select jsonb_array_elements_text($1::jsonb)::bigint) order by id',
                            [json.dumps([int(i) for i in event_ids])])
    for r in rows:
        r['detail'] = store._j_load(r['detail']) or {}
    return rows


def enabled() -> bool:
    return os.environ.get('SCHEDULER', '').lower() == 'on'


async def every_minute(_app):
    """The web app's cleanup context (web/server.py): one loop in one process, only where SCHEDULER=on."""
    async def loop():
        while True:
            try:
                if octo.ready():
                    done = await run_once()
                    if done.get('calls') or done.get('skipped'):
                        print('scheduler', done, flush=True)
            except Exception as e:
                print('scheduler failed', type(e).__name__, e, flush=True)
            await asyncio.sleep(EVERY)
    task = asyncio.ensure_future(loop()) if enabled() else None
    yield
    if task:
        task.cancel()
