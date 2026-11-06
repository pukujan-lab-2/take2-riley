"""The scheduler, without a database or a phone: the properties of track 11 (docs/BUILD-PLAN.md, "Properties and
proof"). The dialler is always a fake; nothing here can ring the trunk.

- nothing is called before it is due, and every item a call carries is marked done after it;
- outside immediate mode no call starts between 9 pm and 8 am their time, in every zone and across DST;
- nobody is called without consent; with nothing on file only the callback they asked for;
- one person's updates due the same day go out in one call; a callback is its own; nothing due is lost;
- a date update goes out inside calling hours before the promise passes, and moves it later;
- a callback time is never in the past and lands on the day and time they said;
- a callback answered or missed writes `callback {answered}`; a failed dial writes no signal;
- what Riley says names only the dates and roles of its records.
"""
import contextlib
import functools
import json
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from hypothesis import given, settings
from hypothesis import strategies as st

from riley import followup, outbound, outcome, scheduler, seed
from riley.main import config

ZONES = ['America/New_York', 'America/Chicago', 'America/Denver', 'America/Phoenix', 'America/Los_Angeles',
         'America/Anchorage', 'Pacific/Honolulu']
NOW = datetime(2026, 11, 6, 15, tzinfo=UTC)   # Friday, 10 am in New York
moments = st.datetimes(min_value=datetime(2026, 1, 1), max_value=datetime(2027, 12, 31),
                       timezones=st.just(UTC))
zones = st.sampled_from(ZONES)
consents = st.sampled_from([{}, {'calls': True}, {'calls': False}, {'other_roles': True}])
kinds = st.sampled_from(['callback_due', 'outcome_queued', 'reminder', 'no_show_follow_up', 'date_update'])
APPROVED = {'status': 'approved', 'delivered': None}


def person(tz='America/New_York', consent=None, guest=False, phone='6465552368', sandbox='live'):
    return {'timezone': tz, 'consent': json.dumps(consent if consent is not None else {'calls': True}),
            'guest': '{"phone": "6465552368"}' if guest else None, 'phone': phone, 'sandbox': sandbox}


def item(kind, due, cid=1, eid=1, app=10, **detail):
    it = {'kind': kind, 'event_id': eid, 'sandbox': 'live', 'candidate_id': cid, 'application_id': app, 'due': due,
          'detail': detail}
    if kind == 'outcome_queued':
        it.update(message_id=100 + eid, message=APPROVED)
    return it


def every(p):
    return [i for c in p['calls'] for i in c['items']]


# ---- due, quiet hours and consent ----

@settings(max_examples=200, deadline=None)
@given(now=moments, offsets=st.lists(st.integers(-600, 600), min_size=1, max_size=6),
       mode=st.sampled_from(outcome.MODES))
def test_nothing_is_called_before_it_is_due(now, offsets, mode):
    items = [item('reminder', now + timedelta(minutes=m), eid=i, at=(now + timedelta(days=1)).isoformat())
             for i, m in enumerate(offsets)]
    p = scheduler.plan(items, {1: person()}, {'live': mode}, now)
    due = [i for i in items if i['due'] <= now]
    if not due:
        assert p == {'calls': [], 'skip': [], 'wait': []}
    # Only today's later updates ride along with one that is due now
    for i in every(p):
        assert i['due'] <= now or i['due'] <= scheduler.end_of_day(now, 'America/New_York')
    assert all(i['due'] <= now for i in p['wait'])


@settings(max_examples=300, deadline=None)
@given(now=moments, tz=zones, kind=kinds, mode=st.sampled_from(outcome.MODES))
def test_no_call_in_quiet_hours_except_in_immediate_mode(now, tz, kind, mode):
    p = scheduler.plan([item(kind, now - timedelta(minutes=5), at=(now + timedelta(days=1)).isoformat())],
                       {1: person(tz)}, {'live': mode}, now)
    t = now.astimezone(ZoneInfo(tz)).time()
    quiet = t.hour >= 21 or t.hour < 8
    if quiet and mode != 'immediate':
        assert not p['calls'] and len(p['wait']) == 1
    else:
        assert len(p['calls']) == 1


def test_quiet_hours_hold_across_the_clock_changes():
    # 2026-11-01 01:30 New York happens twice; 2027-03-14 02:30 never happens. Both are quiet either way
    for at in [datetime(2026, 11, 1, 5, 30, tzinfo=UTC), datetime(2026, 11, 1, 6, 30, tzinfo=UTC),
               datetime(2027, 3, 14, 7, 30, tzinfo=UTC)]:
        assert not scheduler.may_call_now(at, 'America/New_York', 'normal')
    assert scheduler.may_call_now(datetime(2026, 11, 1, 13, tzinfo=UTC), 'America/New_York', 'normal')


@settings(max_examples=300, deadline=None)
@given(consent=consents, guest=st.booleans(), kind=kinds)
def test_nobody_is_called_without_consent(consent, guest, kind):
    p = scheduler.plan([item(kind, NOW - timedelta(minutes=1), at=(NOW + timedelta(days=1)).isoformat())],
                       {1: person(consent=consent, guest=guest)}, {'live': 'normal'}, NOW)
    called = bool(p['calls'])
    if consent.get('calls') is False:
        assert not called and p['skip'][0][1] == 'said no to calls'
    elif consent.get('calls') is True or guest:
        assert called
    else:  # nothing on file: only the callback they asked for
        assert called == (kind == 'callback_due')
    assert called or p['skip']


def test_no_phone_or_a_message_already_given_is_skipped_with_the_reason():
    p = scheduler.plan([item('reminder', NOW, at=NOW.isoformat())], {1: person(phone=None)}, {}, NOW)
    assert p['skip'][0][1] == 'no phone on file'
    given_ = {**item('outcome_queued', NOW), 'message': {'status': 'delivered', 'delivered': NOW}}
    p = scheduler.plan([given_], {1: person()}, {}, NOW)
    assert not p['calls'] and 'already given' in p['skip'][0][1]


@functools.cache
def _cast() -> list[dict]:
    return seed.cast(seed.demo_jobs(), NOW)


@settings(max_examples=200, deadline=None)
@given(now=moments, mode=st.sampled_from(outcome.MODES), consent=consents, guest=st.booleans(), tz=zones,
       ks=st.lists(kinds, min_size=1, max_size=5), offsets=st.lists(st.integers(-600, 0), min_size=5, max_size=5),
       who=st.sampled_from(['cast', 'bias', 'fiction']), last=st.integers(0, 99))
def test_a_made_up_person_or_a_555_01_number_is_never_called(now, mode, consent, guest, tz, ks, offsets, who, last):
    """The guest cast (riley/seed.py), the bias seed's people and any 555-01xx number: skipped every time, with the
    reason, whatever their consent, the mode, the hour or the kind of item."""
    cast = _cast()
    phone = cast[last % len(cast)]['phone'] if who == 'cast' else None if who == 'bias' else f'64655501{last:02d}'
    p = {**person(tz, consent, guest, phone=phone or '6465552368'), 'sandbox': 'guest-0123456789ab',
         'demo': {'cast': _cast()[0]['facts']['demo'], 'bias': 'bias'}.get(who)}
    items = [{**item(k, now + timedelta(minutes=m), eid=i, at=(now + timedelta(days=1)).isoformat()),
              'sandbox': p['sandbox']} for i, (k, m) in enumerate(zip(ks, offsets, strict=False))]
    for it in items:
        if it['kind'] == 'date_update':
            it.update(event_id=None, app={'promised_by': (now + timedelta(hours=10)).isoformat()})
    out = scheduler.plan(items, {1: p}, {p['sandbox']: mode}, now)
    assert out['calls'] == [] and out['wait'] == []
    assert len(out['skip']) == len(items)
    assert {why for _, why in out['skip']} <= {'made-up demo person: never called', 'a made-up 555-01xx number'}


# ---- combining ----

@settings(max_examples=300, deadline=None)
@given(now=moments, tz=zones,
       spec=st.lists(st.tuples(st.integers(1, 3), kinds, st.integers(-300, 900)), min_size=1, max_size=10))
def test_one_call_per_person_per_day_and_nothing_due_is_lost(now, tz, spec):
    items = [item(k, now + timedelta(minutes=m), cid=c, eid=i, at=(now + timedelta(days=2)).isoformat())
             for i, (c, k, m) in enumerate(spec)]
    people = {c: person(tz) for c in (1, 2, 3)}
    p = scheduler.plan(items, people, {'live': 'immediate'}, now)
    for c in (1, 2, 3):
        mine = [x for x in p['calls'] if x['candidate_id'] == c]
        assert len(mine) <= 1, 'one call per person per run'
        for call in mine:
            if call['mode'] == 'callback':
                assert {scheduler.kind_of(i) for i in call['items']} == {'callback'}
            else:
                assert all(scheduler.kind_of(i) in scheduler.UPDATES for i in call['items'])
                assert sum(scheduler.kind_of(i) == 'outcome' for i in call['items']) <= 1
                day = now.astimezone(ZoneInfo(tz)).date()
                assert all(i['due'].astimezone(ZoneInfo(tz)).date() <= day for i in call['items'])
    ids = [i['event_id'] for i in every(p)] + [i['event_id'] for i, _ in p['skip']] + [i['event_id'] for i in p['wait']]
    assert len(ids) == len(set(ids)), 'no item in two places'
    due = {i['event_id'] for i in items if i['due'] <= now}
    assert due <= set(ids), 'every due item is called, skipped or waiting'


def test_two_updates_on_one_day_are_one_follow_up_call():
    items = [item('reminder', NOW - timedelta(minutes=1), eid=1, at='2026-11-09T15:00:00+00:00'),
             item('no_show_follow_up', NOW + timedelta(hours=5), eid=2, app=11, at='2026-11-05T15:00:00+00:00'),
             item('reminder', NOW + timedelta(days=1), eid=3, at='2026-11-10T15:00:00+00:00')]
    p = scheduler.plan(items, {1: person()}, {'live': 'normal'}, NOW)
    (call,) = p['calls']
    assert call['mode'] == 'follow_up' and [i['event_id'] for i in call['items']] == [1, 2]
    assert scheduler.metadata(call) == {'mode': 'follow_up', 'candidate_id': 1, 'application_id': 10,
                                        'event_ids': [1, 2]}
    # With an outcome due the same day, the call is the outcome call and the rest are said after it
    items.append(item('outcome_queued', NOW + timedelta(hours=1), eid=4, app=12))
    (call,) = scheduler.plan(items, {1: person()}, {'live': 'normal'}, NOW)['calls']
    assert scheduler.metadata(call) == {'mode': 'outcome', 'candidate_id': 1, 'application_id': 12,
                                        'message_id': 104, 'event_ids': [1, 2]}


def test_a_callback_is_its_own_call_and_nobody_is_rung_twice_in_half_an_hour():
    items = [item('callback_due', NOW, eid=1, said='tomorrow after 5'),
             item('reminder', NOW, eid=2, at=NOW.isoformat())]
    p = scheduler.plan(items, {1: person(consent={})}, {}, NOW)
    assert [c['mode'] for c in p['calls']] == ['callback'] and [i['event_id'] for i in p['wait']] == [] \
        and p['skip'][0][0]['event_id'] == 2   # no consent for the reminder; the callback they asked for goes
    p = scheduler.plan(items, {1: person()}, {}, NOW, last_call={1: NOW - timedelta(minutes=10)})
    assert not p['calls'] and len(p['wait']) == 2


# ---- date updates ----

@settings(max_examples=400, deadline=None)
@given(promised=moments, tz=zones, status=st.sampled_from(['applied', 'screened']), window=st.booleans())
def test_a_date_update_goes_out_in_calling_hours_before_the_promise_passes(promised, tz, status, window):
    due = scheduler.update_due(promised)
    first = outcome.next_allowed(due, tz)
    assert due < promised and first < promised and scheduler.may_call_now(first, tz, 'normal')
    app = {'promised_by': promised.isoformat(), 'status': status, 'window_hours': 48,
           'first_applied': (promised - timedelta(hours=10 if window else 100)).isoformat()}
    d = scheduler.date_update(app, due, 'Dana')
    assert datetime.fromisoformat(d['to']) > promised and d['reason']


def test_the_reason_comes_from_the_record():
    base = {'promised_by': '2026-11-07T20:00:00+00:00', 'window_hours': 48}
    assert 'phone screen' in scheduler.date_update({**base, 'status': 'applied'}, NOW, 'Dana')['reason']
    held = {**base, 'status': 'screened', 'first_applied': '2026-11-07T00:00:00+00:00'}
    assert 'review window' in scheduler.date_update(held, NOW, 'Dana')['reason']
    late = {**base, 'status': 'screened', 'first_applied': '2026-11-01T00:00:00+00:00'}
    assert scheduler.date_update(late, NOW, 'Dana')['reason'].startswith('Dana needs more time')


def test_a_reminder_is_a_day_before_or_two_hours_before():
    at = NOW + timedelta(days=3)
    assert scheduler.reminder_due(at, NOW) == at - timedelta(days=1)
    assert scheduler.reminder_due(NOW + timedelta(hours=5), NOW) == NOW + timedelta(hours=3)
    assert scheduler.reminder_due(NOW + timedelta(hours=1), NOW) is None


# ---- callback times ----

said = st.sampled_from(['tomorrow after 5', 'Monday morning', 'today at 3:30 pm', 'in two hours', 'tonight',
                        'after six', 'this evening', 'Thursday at 11', 'later', 'whenever', 'at noon', 'in 45 minutes',
                        'sunday afternoon', 'tomorrow', 'at 9am', ''])


@settings(max_examples=300, deadline=None)
@given(now=moments, tz=zones, words=said)
def test_a_callback_time_is_never_in_the_past(now, tz, words):
    at = scheduler.parse_when(words, now, tz)
    assert at is None or at > now
    assert scheduler.callback_time(words, now, tz) > now


def test_a_day_and_time_they_said_lands_on_that_day_and_time():
    ny = ZoneInfo('America/New_York')

    def at(words, now=NOW):
        t = scheduler.parse_when(words, now, 'America/New_York')
        return t.astimezone(ny).strftime('%a %-d %H:%M') if t else None
    assert at('tomorrow after 5') == 'Sat 7 17:00'
    assert at('tomorrow after five') == 'Sat 7 17:00'
    assert at('Monday morning') == 'Mon 9 10:00'
    assert at('today at 3:30 pm') == 'Fri 6 15:30'
    assert at('Friday at 11') == 'Fri 6 11:00'   # today's Friday: 11 am is still ahead
    assert at('Thursday at 11') == 'Thu 12 11:00'
    assert at('Friday at 9') == 'Fri 13 09:00'  # said on Friday after 9: next Friday
    assert at('in two hours') == 'Fri 6 12:00'
    assert at('today at 9 am') is None and at('whenever') is None and at('call me back') is None
    # Words with no time at all: (default) the next morning at 10
    assert scheduler.callback_time('later', NOW, 'America/New_York').astimezone(ny).strftime('%a %H:%M') == 'Sat 10:00'
    # Their zone: "tomorrow after 5" from Los Angeles is 5 pm there
    la = scheduler.parse_when('tomorrow after 5', NOW, 'America/Los_Angeles')
    assert la and la.astimezone(ZoneInfo('America/Los_Angeles')).hour == 17


# ---- after a call ----

@given(answered=st.sampled_from([True, False, None]), mode=st.sampled_from(['callback', 'follow_up']))
def test_a_callback_writes_answered_and_a_failed_dial_writes_no_signal(answered, mode):
    call = {'candidate_id': 1, 'sandbox': 'live', 'application_id': 10, 'message_id': None, 'mode': mode,
            'items': [item('callback_due' if mode == 'callback' else 'reminder', NOW, eid=5, at=NOW.isoformat())]}
    result = {'placed': answered is True, 'why': 'x', 'answered': answered}
    writes = scheduler.after_dial(call, result)
    assert ('done', 5, {'placed': answered is True, 'why': 'x', 'answered': answered}) in writes
    signals = [w for w in writes if w[0] == 'log' and w[2] == 'callback']
    if mode == 'callback' and answered is not None:
        assert [w[5]['answered'] for w in signals] == [answered]
    else:
        assert not signals
    assert not any(w[0] == 'log' and w[2] == 'no_show' for w in writes), 'a missed call is never a no-show'


# ---- what Riley says ----

@settings(max_examples=200, deadline=None)
@given(at=moments, tz=zones, kind=st.sampled_from(['reminder', 'no_show_follow_up', 'date_moved']),
       title=st.sampled_from(['RN, Telemetry', 'RN, Medical-Surgical', 'LPN, Rehab']), paused=st.booleans())
def test_follow_up_lines_name_only_the_dates_and_roles_of_their_records(at, tz, kind, title, paused):
    detail = {'title': title, 'at': at.isoformat(), 'with': 'nurse', 'paused': paused, 'to': at.isoformat(),
              'reason': 'Dana needs more time to read every screen for this role'}
    (line,) = scheduler.lines([{'kind': kind, 'detail': detail}], 'Dana', tz)
    d = at.astimezone(ZoneInfo(tz))
    low = line.lower()
    assert title in line
    assert {w for w in outcome.WEEKDAYS if re.search(rf'\b{w}\b', low)} == {d.strftime('%A').lower()}
    assert {m for m in outcome.MONTHS if re.search(rf'\b{m}\b', low)} == {d.strftime('%B').lower()}
    assert str(d.day) in re.findall(r'\b\d+\b', line)


def test_a_follow_up_says_nothing_before_they_confirm_who_they_are():
    cfg = config()
    items = [{'kind': 'no_show_follow_up', 'event_id': 1, 'candidate_id': 1, 'application_id': 2,
              'detail': {'title': 'RN, Telemetry', 'at': '2026-11-05T15:00:00+00:00', 'paused': False}}]
    state = followup.FollowUp(cfg, items, scheduler.lines(items, 'Dana'))
    hidden = followup.instructions(cfg, state, hidden=True)
    assert 'Telemetry' not in hidden and 'missed' not in hidden.lower()
    full = followup.instructions(cfg, state)
    assert state.lines[0] in full and 'Never ask why' in full
    # Their reason is never said back, not even softened, and the update comes before the question
    assert 'something came up' in full and 'before you ask' in full


# ---- the dialler ----

class FakeLiveKit:
    def __init__(self, fail=None):
        self.calls, self.fail = [], fail
        part = lambda names: SimpleNamespace(**{n: self._rec(n) for n in names})  # noqa: E731
        self.room, self.agent_dispatch = part(['create_room']), part(['create_dispatch'])
        self.sip = part(['create_sip_participant'])

    def _rec(self, name):
        async def f(req):
            self.calls.append((name, req))
            if name == 'create_sip_participant' and self.fail:
                raise self.fail
        return f


class SipError(Exception):
    def __init__(self, code):
        super().__init__(f'sip {code}')
        self.sip_status_code = code


async def test_waiting_for_an_answer_says_whether_they_picked_up(monkeypatch):
    monkeypatch.setenv('SIP_OUTBOUND_TRUNK_ID', 'ST_test')
    lk = FakeLiveKit()
    meta = {'mode': 'follow_up', 'candidate_id': 1, 'application_id': 2, 'event_ids': [3, 4]}
    out = await outbound.place('6465552368', meta, lk=lk, wait=True)
    assert out['answered'] is True and lk.calls[2][1].wait_until_answered
    assert json.loads(lk.calls[0][1].metadata)['event_ids'] == [3, 4]
    out = await outbound.place('6465552368', meta, lk=FakeLiveKit(SipError(486)), wait=True)
    assert out == {'placed': False, 'why': 'dial failed: SipError', 'answered': False}
    out = await outbound.place('6465552368', meta, lk=FakeLiveKit(RuntimeError('trunk down')), wait=True)
    assert out['answered'] is None, 'a failed dial says nothing about the person'


async def test_the_loop_is_off_unless_switched_on_and_never_dials_without_a_trunk(monkeypatch):
    monkeypatch.delenv('SCHEDULER', raising=False)
    assert not scheduler.enabled()
    monkeypatch.setenv('SCHEDULER', 'on')
    assert scheduler.enabled()
    monkeypatch.delenv('SIP_OUTBOUND_TRUNK_ID', raising=False)
    assert (await scheduler.run_once(NOW))['calls'] == 0
    gen = scheduler.every_minute(None)
    monkeypatch.setenv('SCHEDULER', 'off')
    await gen.__anext__()   # off: no loop task is started
    with contextlib.suppress(StopAsyncIteration):
        await gen.__anext__()


def test_a_demo_outcome_skips_the_gap_and_waits_for_ring_after():
    later = (NOW + timedelta(minutes=3)).isoformat()
    m = {'status': 'approved', 'delivered': None, 'parts': {'ring_after': later}}
    assert outcome.ring_after(m, NOW) and not outcome.ring_after(m, NOW + timedelta(minutes=4))
    it = item('outcome_queued', NOW, eid=3)
    p = scheduler.plan([it], {1: person()}, {'live': 'immediate'}, NOW, last_call={1: NOW - timedelta(minutes=5)})
    assert [c['mode'] for c in p['calls']] == ['outcome']
    p = scheduler.plan([it], {1: person()}, {'live': 'normal'}, NOW, last_call={1: NOW - timedelta(minutes=5)})
    assert not p['calls']
