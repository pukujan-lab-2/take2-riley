"""Guests from /try (track 9), without a model, a database or a phone line: who may be called and when, what the
two-day purge removes, and that the guest's own name and number never reach the call."""
import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from riley import guest, guest_call, outbound
from riley.main import config
from riley.screener import Screen, Screener, job_config

# No per-example deadline: CI and a busy laptop run slower than Hypothesis's 200 ms default
settings.register_profile('guest', deadline=None)
settings.load_profile('guest')

NOW = datetime(2026, 11, 6, 18, tzinfo=UTC)
CFG = config()
PHONE = '(646) 555-2368'
hours = st.floats(min_value=0, max_value=24 * 7, allow_nan=False)


def ok(**kw):
    args = {'now': NOW, 'consent': True, 'phone': PHONE, 'number_calls': [], 'ip_calls': [], 'today': 0, 'cfg': CFG}
    return guest.refuse(**{**args, **kw})


def test_a_fresh_request_goes_ahead():
    assert ok() is None


@given(phone=st.text(max_size=20), calls=st.lists(hours, max_size=5), today=st.integers(0, 1000))
def test_no_call_without_the_checkbox(phone, calls, today):
    assert ok(consent=False, phone=phone, number_calls=[NOW - timedelta(hours=h) for h in calls], today=today)


@given(ago=hours)
def test_a_number_is_called_at_most_once_a_day(ago):
    why = ok(number_calls=[NOW - timedelta(hours=ago)])
    assert (why is not None) == (ago < guest.NUMBER_HOURS)


def test_visitor_and_daily_limits():
    per_ip, cap = CFG['guests']['per_ip_hour'], CFG['guests']['daily_cap']
    assert ok(ip_calls=[NOW - timedelta(minutes=5)] * per_ip)
    assert ok(ip_calls=[NOW - timedelta(hours=2)] * per_ip) is None
    assert ok(today=cap) and ok(today=cap - 1) is None


@pytest.mark.parametrize('typed', ['6465552368', '+1 646 555 2368', '1-646-555-2368', '(646) 555-2368'])
def test_spellings_of_one_us_number_agree(typed):
    assert guest.us_mobile(typed) == '6465552368'


@pytest.mark.parametrize('bad', ['', '123', '0465552368', '6461552368', '2125550123', '+44 20 7946 0958', None])
def test_not_a_dialable_us_number(bad):
    assert guest.us_mobile(bad) is None


row = st.fixed_dictionaries({
    'id': st.integers(1, 10**6), 'name': st.sampled_from(guest.NAMES),
    'phone': st.from_regex(r'[2-9]\d{9}', fullmatch=True), 'email': st.none() | st.emails(),
    'language': st.sampled_from(['en', 'es']), 'facts': st.dictionaries(st.text(), st.text()),
    'guest': st.fixed_dictionaries({'first_name': st.text(max_size=10), 'phone': st.text(max_size=10)}),
    'expires': st.floats(min_value=-200, max_value=200).map(lambda h: NOW + timedelta(hours=h)),
})


@given(row)
def test_the_purge_removes_phone_and_typed_name_after_two_days_and_nothing_else(r):
    after = guest.scrub(r, NOW)
    if r['expires'] > NOW:
        assert after == r
    else:
        assert after['phone'] is None and after['guest'] is None
        assert {k: v for k, v in after.items() if k not in ('phone', 'guest')} == \
               {k: v for k, v in r.items() if k not in ('phone', 'guest')}


def test_made_up_names_are_never_riley():
    assert all(len(n.split()) == 2 and not n.startswith('Riley') for n in guest.NAMES)


@given(st.dictionaries(st.sampled_from(['mode', 'candidate_id', 'application_id', 'message_id', 'phone', 'name',
                                        'first_name', 'email']), st.integers(1, 99) | st.text(max_size=12)))
def test_room_metadata_holds_only_ids_and_the_mode(meta):
    meta['mode'] = 'guest'
    assert set(json.loads(outbound.room_metadata(meta))) <= {'mode', 'candidate_id', 'application_id', 'message_id'}


class FakeLiveKit:
    def __init__(self):
        self.calls = []
        part = lambda name: SimpleNamespace(**{n: self._rec(n) for n in name})  # noqa: E731
        self.room, self.agent_dispatch = part(['create_room']), part(['create_dispatch'])
        self.sip = part(['create_sip_participant'])

    def _rec(self, name):
        async def f(req):
            self.calls.append((name, req))
        return f


async def test_place_without_a_trunk_dials_nothing(monkeypatch):
    monkeypatch.delenv('SIP_OUTBOUND_TRUNK_ID', raising=False)
    lk = FakeLiveKit()
    assert await outbound.place('+16465552368', {'mode': 'guest', 'application_id': 1}, lk=lk) == \
        {'placed': False, 'why': 'no outbound trunk'}
    assert lk.calls == []


async def test_place_dials_through_the_trunk_and_keeps_the_number_out_of_the_room(monkeypatch):
    monkeypatch.setenv('SIP_OUTBOUND_TRUNK_ID', 'ST_test')
    monkeypatch.setenv('RILEY_CALLER_ID', '+16317090065')
    lk = FakeLiveKit()
    out = await outbound.place('+16465552368', {'mode': 'guest', 'candidate_id': 7, 'application_id': 9,
                                                'phone': '+16465552368'}, lk=lk)
    assert out['placed'] and out['why'].startswith('out-')
    (_, room), (_, dispatch), (_, sip) = lk.calls
    assert room.name == dispatch.room == sip.room_name == out['why'] and dispatch.agent_name == 'nurse-screen'
    assert json.loads(room.metadata) == json.loads(dispatch.metadata) == \
        {'mode': 'guest', 'candidate_id': 7, 'application_id': 9}
    assert sip.sip_trunk_id == 'ST_test' and sip.sip_call_to == '+16465552368' and sip.sip_number == '+16317090065'
    assert '6465552368' not in room.name + room.metadata
    lk = FakeLiveKit()  # the store's ten digits are dialled as E.164
    await outbound.place('6465552368', {'mode': 'outcome', 'message_id': 3}, lk=lk)
    assert lk.calls[2][1].sip_call_to == '+16465552368'


@settings(max_examples=50, deadline=None)
@given(area=st.integers(200, 999), last=st.integers(0, 99),
       spelling=st.sampled_from(['({a}) 555-01{n}', '+1 {a} 555 01{n}', '{a}55501{n}', '1-{a}-555-01{n}']))
def test_place_never_dials_a_made_up_555_01_number_even_with_a_trunk(area, last, spelling):
    lk = FakeLiveKit()
    number = spelling.format(a=area, n=f'{last:02d}')
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv('SIP_OUTBOUND_TRUNK_ID', 'ST_test')
        out = asyncio.run(outbound.place(number, {'mode': 'outcome', 'message_id': 1}, lk=lk))
    assert out == {'placed': False, 'why': 'a made-up 555-01xx number: never dialled'} and lk.calls == []


DEMO = {'name': 'Jordan Lee', 'title': 'RN, ICU', 'hospital': 'Hudson Park Midtown'}


def test_a_guest_call_asks_the_language_first_and_holds_nothing_they_typed():
    agent = Screener(job_config(CFG, {'title': 'RN, ICU', 'questions': '[]'}), demo=DEMO)
    hello = guest_call.greeting(CFG, outbound=True)
    assert hello.endswith('English or Spanish? ¿Inglés o español?') and 'AI assistant' in hello
    assert 'set_language' in {t.info.name for t in agent.tools} and 'confirm_identity' not in agent.instructions
    assert 'RN, ICU' in agent.instructions
    for typed in ('Teresa', '646', '555'):  # the guest's first name and number are never given to the agent
        assert typed not in agent.instructions + hello


async def test_spanish_switches_the_instructions_and_the_line_names_who_they_play():
    agent = Screener(CFG, demo=DEMO)
    tool = next(t for t in agent.tools if t.info.name == 'set_language')
    switched = []
    agent2 = Screener(CFG, demo=DEMO, on_language=switched.append)
    tool2 = next(t for t in agent2.tools if t.info.name == 'set_language')
    seen = []

    async def update(text):
        seen.append(text)
    agent2.update_instructions = update  # type: ignore[method-assign]
    state = Screen(cfg=CFG)
    spoken = []
    session = SimpleNamespace(say=spoken.append)
    result = await tool2(SimpleNamespace(userdata=state, session=session), 'Spanish')
    assert state.language == 'es' and switched == ['es'] and seen[0].endswith(guest_call.SPANISH)
    # The code says who they play, in their language, and the model adds nothing to that turn
    assert spoken == ['Para esta demostración, usted es Jordan Lee y solicita el puesto de RN, ICU en '
                      'Hudson Park Midtown. ¿Es correcto?']
    assert result.reply_required is False
    english = Screen(cfg=CFG)
    await tool(SimpleNamespace(userdata=english, session=session), 'en')
    assert english.language == 'en'
    assert spoken[-1] == "For this demo you're Jordan Lee, applying for RN, ICU at Hudson Park Midtown. Is that right?"
