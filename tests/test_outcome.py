"""Outcomes, without a model or a database: the properties of track 4 (docs/BUILD-PLAN.md, "Properties and proof").

- nothing is delivered or rung without Dana's approval, in any mode (rule 1);
- every fact in a message's text is in its parts, and the check refuses one that is not;
- booking accepts only a listed slot, with someone it is open for;
- a caller who is not them hears nothing about the outcome;
- the outcome call has no tool that can change an answer or a score;
- 'normal' never decides on its own and never rings at approval; quiet hours hold in every time zone;
- a job in "Dana confirms every no" never gets an automatic no, and Dana's email links to the optional bias form.
"""
import asyncio
import html
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from livekit.agents.llm import ToolError

from riley import bias, guest, identity, octo, outcome, outcomeline, store
from riley.main import config

NOW = datetime(2026, 11, 6, 15, tzinfo=UTC)
JOB = {'title': 'RN, Medical-Surgical', 'hospital': 'Hudson Park Midtown',
       'slots': ['2026-11-09T15:00:00+00:00', '2026-11-10T19:30:00+00:00',
                 {'at': '2026-11-11T14:00:00+00:00', 'with': 'nurse'}, '2026-11-01T15:00:00+00:00'],
       'options': ['per diem', {'say': 'the evening shift'}]}
BLS = {'id': 4, 'name': 'BLS', 'reason': 'This role needs a current BLS, and yours has lapsed.',
       'change': 'Renew your BLS and call this number; your answers and date are kept.'}
TELE = {'job_id': 12, 'title': 'RN, Telemetry', 'hospital': 'Hudson Park Bronx', 'unit': 'step-down',
        'shift': 'nights'}


def parts(result='not_selected', offers=(TELE,), criteria=(BLS,), reason=''):
    return outcome.parts_for(result, reason, list(criteria), list(offers), JOB, 'Ana Ruiz', config(), NOW)


def test_parts_come_from_the_records():
    p = parts('next_round')
    assert [s['say'] for s in p['slots']] == ['Monday, November 9, at 10 am', 'Tuesday, November 10, at 2:30 pm',
                                              'Wednesday, November 11, at 9 am']  # past slot dropped
    assert p['offers'] == [] or p['result'] == 'next_round'
    q = parts()
    assert q['items'][0]['reason'] == BLS['reason'] and q['options'] == ['per diem', 'the evening shift']
    assert q['slots'] == [] and q['employer'] == 'Hudson Park Health'


@pytest.mark.parametrize('kind', outcome.KINDS)
def test_template_passes_the_fact_check(kind):
    p = parts(kind)
    text = outcome.template(p)
    assert outcome.check(text, p) == [], text
    if kind != 'next_round':
        assert BLS['reason'] in text and BLS['change'] in text and 'RN, Telemetry' in text


facts = st.sampled_from(['ACLS', 'PALS', 'the ICU role', 'Hudson Park Brooklyn', 'Thursday', 'December 3',
                         '$70 an hour', 'CCRN', 'labor and delivery', 'evenings', 'a 15 minute call'])


@settings(max_examples=60, deadline=None)
@given(kind=st.sampled_from(outcome.KINDS), extra=facts, with_offer=st.booleans())
def test_a_fact_not_in_the_parts_is_refused(kind, extra, with_offer):
    """Every fact in the text is in the parts: add one that isn't, anywhere, and the check names it."""
    p = parts(kind, offers=(TELE,) if with_offer else ())
    text = outcome.template(p)
    if extra.lower() in outcome._blob(p):
        return
    assert outcome.check(f'{text} You could also look at {extra}.', p), extra


statuses = st.sampled_from(['draft', 'approved', 'sent', 'delivered', 'dropped'])


@given(status=statuses, delivered=st.booleans(), mode=st.sampled_from(outcome.MODES))
def test_nothing_is_delivered_or_rung_without_approval(status, delivered, mode):
    m = {'status': status, 'delivered': NOW if delivered else None}
    if outcome.can_deliver(m) or outcome.rings_now(mode, m):
        assert status == 'approved' and not delivered
    if mode == 'normal':
        assert not outcome.rings_now(mode, m) and not outcome.auto_decides(mode)


@given(slot=st.text(max_size=40), who=st.sampled_from(['recruiter', 'nurse']))
def test_booking_accepts_only_a_listed_slot(slot, who):
    slots = parts('next_round')['slots']
    got = outcome.pick_slot(slots, slot, who)
    assert got is None or (got in slots and (not got['with'] or got['with'] == who))


def test_booking_matches_the_way_a_slot_is_said():
    slots = parts('next_round')['slots']
    assert outcome.pick_slot(slots, 'monday, November 9, at 10 am', 'recruiter') == slots[0]
    assert outcome.pick_slot(slots, 'Wednesday, November 11, at 9 am', 'recruiter') is None  # nurse only
    assert outcome.pick_slot(slots, 'Wednesday, November 11, at 9 am', 'nurse') == slots[2]
    assert outcome.pick_slot(slots, 'Friday at noon', 'nurse') is None


@settings(max_examples=40)
@given(at=st.datetimes(min_value=datetime(2026, 1, 1), max_value=datetime(2027, 12, 31), timezones=st.just(UTC)),
       tz=st.sampled_from(['America/New_York', 'America/Los_Angeles', 'America/Chicago', 'Pacific/Honolulu']))
def test_queued_outcomes_wait_for_daytime_in_every_time_zone(at, tz):
    due = outcome.next_allowed(at, tz)
    assert due >= at and not outcome.quiet(due, tz) and due - at <= timedelta(hours=12)
    local = due.astimezone(ZoneInfo(tz)).time()
    assert outcome.QUIET_END <= local < outcome.QUIET_START


def test_auto_result_follows_dana_rules():
    advance = {'flags': [], 'next_step': 'advance to interview'}
    assert outcome.auto_result([], advance) == 'next_round'
    assert outcome.auto_result([], {'flags': [], 'next_step': 'recruiter review: screen incomplete'}) == 'not_yet'
    assert outcome.auto_result([BLS], {'flags': ['certifications: no BLS']}) == 'not_yet'
    assert outcome.auto_result([BLS, {**BLS, 'id': 5, 'change': ''}], {'flags': ['x']}) == 'not_selected'
    assert outcome.auto_result([{**BLS, 'change': ''}], {'flags': ['x']}) == 'not_selected'


def test_email_is_signed_by_dana_for_each_kind():
    app = {'title': 'RN, Medical-Surgical', 'name': 'Ana Ruiz'}
    for kind in outcome.KINDS:
        p = parts(kind)
        e = outcome.email({'kind': kind, 'parts': p, 'text': outcome.template(p)}, app)
        assert e['text'].rstrip().endswith('Dana\nRecruiter, Hudson Park Health')
        assert 'RN, Medical-Surgical' in e['subject'] and '<p>' in e['html']
    raw = outcome.eml({'kind': 'not_yet', 'parts': parts('not_yet'), 'text': 'x'}, app)
    assert b'Subject:' in raw and b'text/html' in raw


def test_samples_match_the_email_function():
    """docs/samples/ holds one email of each kind, made by `outcome.email` (tests/make_samples.py)."""
    from tests.make_samples import SAMPLES, render
    for name, (kind, offers) in SAMPLES.items():
        p = parts(kind, offers=(TELE,) if offers is None else offers)
        assert render(kind, p) == (outcome.ROOT / 'docs' / 'samples' / name).read_text(), name


WHO = {'name': 'Ana Ruiz', 'title': 'RN, Medical-Surgical'}


def line(kind='not_selected'):
    p = parts(kind)
    msg = {'id': 31, 'kind': kind, 'parts': p, 'text': outcome.template(p), 'status': 'approved', 'sandbox': 't'}
    state = outcomeline.Outcome(config(), msg, {'id': 7, 'candidate_id': 3}, language='en')
    return outcomeline.OutcomeLine(config(), state, WHO), state


@pytest.mark.parametrize('kind', outcome.KINDS)
def test_a_caller_who_is_not_them_hears_nothing(kind):
    agent, state = line(kind)
    for leak in [BLS['reason'], 'Telemetry', 'November 9', 'per diem', 'second interview', 'Good news', 'Midtown']:
        assert leak not in agent.instructions, leak
    with pytest.raises(ToolError):
        identity.check(state)


def test_outcome_call_cannot_change_answers_or_score():
    agent, _ = line()
    names = {t.info.name for t in agent.tools}
    # fix_fact only sends the correction to Dana and reopens the review; nothing on the call rewrites an answer
    assert names >= {'confirm_identity', 'book_slot', 'add_role', 'give_feedback', 'request_recruiter', 'fix_fact'}
    assert not names & {'record_answer', 'confirm_resume', 'withdraw'}
    assert set(outcome.STATUS_AFTER.values()) <= {'interview', 'not_yet', 'not_selected'}


def test_dana_has_a_line_for_each_kind():
    lines = outcome.dana_lines(config())
    assert set(lines) == set(outcome.KINDS) and all(len(t.split()) < 30 for t in lines.values())


@given(result=st.sampled_from(outcome.KINDS), on_job=st.booleans(), check_flags=st.booleans())
def test_a_flagged_job_never_gets_an_automatic_no(result, on_job, check_flags):
    """auto() asks the bias check before any automatic no: a not-yet or not-selected on a job in "Dana confirms
    every no" (already set, or set by the check that runs now) stays a draft with an auto_held event. A yes, or a
    job nobody flagged, is approved and rung as before."""
    got = run_auto(result, confirm_every_no=on_job, check_flags=check_flags)
    hold = result != 'next_round' and (on_job or check_flags)
    assert got['approved'] == (not hold)
    assert got['held'] == hold and got['returned']['status'] == ('draft' if hold else 'approved')
    # A yes never waits and a job already set needs no fresh check
    assert got['checked'] == (result != 'next_round' and not on_job)
    # A held draft is not an outcome call, so the guest still gets the "you're in process" call
    assert guest.needs_status_call(got['returned']) == hold


def test_a_bias_check_that_cannot_run_holds_the_no():
    got = run_auto('not_selected', confirm_every_no=False, check_flags=RuntimeError('octo down'))
    assert got['held'] and not got['approved']


def run_auto(result, confirm_every_no, check_flags):
    """outcome.auto() against stand-ins for Octo, the decision and the bias check; nothing real is called."""
    seen = {'approved': False, 'held': False, 'checked': False}
    app = {'id': 7, 'candidate_id': 3, 'job_id': '11', 'title': 'RN, Telemetry', 'slots': None, 'score': {},
           'candidate_facts': {}, 'decision': None, 'confirm_every_no': confirm_every_no}

    async def mode(_):
        return 'immediate'

    async def application(_):
        return app

    async def query(sql, params=None):
        return []

    async def decide(*a, **k):
        return {'id': '21', 'status': 'draft', 'parts': {}, 'kind': result}

    async def approve(mid, by):
        seen['approved'] = True
        return {'id': mid, 'status': 'approved'}

    async def log(_sandbox, kind, *a, **k):
        seen['held'] |= kind == 'auto_held'

    async def confirms(sandbox, job_id):
        seen['checked'] = True
        assert str(job_id) == '11'
        if isinstance(check_flags, Exception):
            raise check_flags
        return check_flags

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(outcome, 'get_mode', mode)
        mp.setattr(outcome, 'application', application)
        mp.setattr(outcome, 'decide', decide)
        mp.setattr(outcome, 'approve', approve)
        mp.setattr(outcome, 'auto_result', lambda short, score: result)
        mp.setattr(outcome, 'track', lambda name: None)
        mp.setattr(outcome.octo, 'query', query)
        mp.setattr(outcome.store, 'log', log)
        mp.setattr(bias, 'confirms_every_no', confirms)
        seen['returned'] = asyncio.run(outcome.auto('live', 7))
    return seen


@given(kind=st.sampled_from(outcome.KINDS), candidate=st.integers(1, 10**9),
       sandbox=st.sampled_from(['live', 'guest-0123456789ab']))
def test_dana_email_has_one_quiet_link_to_their_own_self_id_form(kind, candidate, sandbox):
    """The same optional link as the /try page, for this person only, once, after Dana's words and before her name;
    nothing else from the bias check is in the email."""
    p = parts(kind)
    e = outcome.email({'kind': kind, 'parts': p, 'text': outcome.template(p), 'sandbox': sandbox},
                      {'title': 'RN, Medical-Surgical', 'name': 'Ana Ruiz', 'candidate_id': candidate})
    link = '/self-id?token=' + bias.token(sandbox, candidate)
    assert e['text'].count('/self-id?token=') == 1 and link in e['text']
    assert e['html'].count('/self-id?token=') == 1 and html.escape(link) in e['html']
    assert bias.read_token(link.split('token=')[1]) == (sandbox, candidate)
    assert e['text'].index(link) > e['text'].index('Reply to this email')
    assert e['text'].rstrip().endswith('Dana\nRecruiter, Hudson Park Health')
    assert 'optional' in e['text'].lower()
    assert not any(w in e['text'] for q in bias.QUESTIONS.values() for w in q['options'] if w != bias.DECLINE)


ANSWERS = {'license': {'value': 'No active NY RN license', 'meets': 'no'},
           'years_rn': {'value': 'One year as an RN', 'meets': 'yes'},
           'certifications': {'value': 'BLS current, no ACLS', 'meets': 'no'},
           'per_diem': {'value': 'Per diem schedule does not work', 'meets': 'no'}}
SHORT = [{'id': 125, 'name': 'New York RN license', 'target': {'license': 'NY RN'},
          'reason': 'This role needs an active New York State RN license.', 'change': ''},
         {'id': 127, 'name': 'current ACLS', 'target': {'cert': 'ACLS'},
          'reason': 'This role needs a current ACLS certification.', 'change': ''},
         {'id': 128, 'name': '2 years as an RN', 'target': {'years': 2},
          'reason': 'Floating needs at least two years of acute care.', 'change': ''},
         {'id': 129, 'name': 'per diem', 'target': {'shift': 'per diem'}, 'reason': 'This role is per diem only.',
          'change': ''}]


def test_each_reason_carries_what_they_said_on_the_call():
    """Sam's not-selected from the Nov 6 test call: every reason is tied to their own answer, word for word."""
    said = [outcome.said_for(c, ANSWERS, {})['heard'] for c in SHORT]
    assert said == ['No active NY RN license', 'BLS current, no ACLS', 'One year as an RN',
                    'Per diem schedule does not work']


def test_a_resume_that_met_it_and_a_call_that_did_not_is_named_as_a_difference():
    resume = {'certs': ['BLS', 'ACLS'], 'licenses': ['NY RN'], 'years': 3, 'shifts': []}
    got = {c['id']: outcome.said_for(c, ANSWERS, resume)['resume_differs'] for c in SHORT}
    # ACLS and the license: the resume had them, the call said no. Years: the call said yes, so no difference to ask
    assert got == {125: True, 127: True, 128: False, 129: False}


def test_the_message_and_the_help_line_give_the_reasons_and_a_way_to_correct_them():
    resume = {'certs': ['BLS', 'ACLS'], 'licenses': [], 'years': 1, 'shifts': []}
    p = outcome.parts_for('not_selected', '', SHORT, [], {'title': 'RN, Per diem float'}, 'Sam Rivera', config(),
                          answers=ANSWERS, resume=resume)
    text = outcome.template(p)
    assert 'On the call you said: No active NY RN license.' in text
    assert 'Your resume showed current ACLS, but on the call you said: BLS current, no ACLS.' in text
    assert 'If any of that is wrong' in text and not outcome.check(text, p)
    why = store.why(p)
    assert 'on the call they said One year as an RN' in why and 'resume showed current ACLS' in why


# ---- other roles: never a closed door (owner's test call, 6 Nov: "we don't currently have other openings") ----

def test_a_message_with_no_other_roles_leaves_the_door_open():
    p = parts(offers=())
    text = outcome.template(p)
    assert outcome.OPEN_DOOR in text and outcome.check(text, p) == []
    # the line the draft model wrote on the owner's call is refused, so the template goes out instead
    for closed in ["We don't currently have other openings to offer you.",
                   "I'm sorry, we can't consider you for another position.", 'There are no other roles right now.']:
        assert 'says no other roles' in outcome.check(f'{text} {closed}', p), closed
    assert 'never say there are no other openings' in outcome.DRAFT


def test_the_outcome_call_never_says_it_cannot_consider_them_for_another_role():
    agent, state = line()
    state.identity = 'confirmed'
    full = outcomeline.instructions(config(), state.message, 'en')
    assert "you never say there are no other openings, that you can't consider them" in full
    assert 'call other_roles' in full and 'consider them for other roles as they open' in full
    assert 'other_roles' in {t.info.name for t in agent.tools}


def call_line(found):
    p = parts(offers=())
    msg = {'id': 31, 'kind': 'not_selected', 'parts': p, 'text': outcome.template(p), 'status': 'approved',
           'sandbox': 't'}
    state = outcomeline.Outcome(config(), msg, {'id': 7, 'candidate_id': 3, 'job_id': 5}, language='en')
    state.identity = 'confirmed'
    asked = []

    async def finder(sandbox, candidate_id, exclude):
        asked.append((sandbox, candidate_id, exclude))
        return found
    agent = outcomeline.OutcomeLine(config(), state, WHO, finder=finder)
    tool = {t.info.name: t for t in agent.tools}
    return agent, state, tool, asked


async def test_asking_about_other_roles_offers_the_ones_that_fit():
    from types import SimpleNamespace
    _, state, tool, asked = call_line([TELE])
    said = await tool['other_roles'](SimpleNamespace(userdata=state), 'anything on nights')
    assert asked == [('t', 3, 5)] and 'RN, Telemetry' in said and '[12]' in said
    await tool['add_role'](SimpleNamespace(userdata=state), 12)   # a role it found is one they can say yes to
    assert state.add_roles == [12] and state.other_roles and not state.requests


async def test_with_no_role_that_fits_their_interest_is_kept_for_dana():
    from types import SimpleNamespace
    _, state, tool, _ = call_line([])
    said = await tool['other_roles'](SimpleNamespace(userdata=state), '')
    assert "never say they can't be considered" in said and 'as they open' in said
    assert state.requests == [{'kind': 'add_role', 'detail': {'role': 'any role that fits', 'from': 'outcome call'}}]
    with pytest.raises(ToolError):
        await tool['add_role'](SimpleNamespace(userdata=state), 99)


def test_asking_about_other_roles_on_the_call_is_kept_as_their_yes(monkeypatch):
    sql = []

    async def query(q, params=None):
        sql.append((q, params))
        return [{'application_id': 7, 'kind': 'not_selected', 'sandbox': 't', 'facts': {}}]
    monkeypatch.setattr(octo, 'query', query)
    _, state, _, _ = call_line([])
    state.other_roles = True
    asyncio.run(outcomeline.save('t', state))
    consent = [p for q, p in sql if 'consent = consent ||' in q]
    assert consent and '"other_roles": true' in consent[0][2]
    assert not any('answers' in q or 'score =' in q for q, _ in sql)


def test_renewing_on_the_outcome_call_goes_to_dana_as_a_fix():
    _, state = line()
    full = outcomeline.instructions(config(), state.message, 'en')
    assert 'being renewed' in full and 'renewal date or the new card' in full and 'call fix_fact' in full
    assert 'already expired or lapsed still does not meet the role' in full


def test_without_matching_a_missing_license_is_not_read_as_missing_years():
    crit = [{'id': 82, 'kind': 'required', 'name': 'New York RN license'},
            {'id': 86, 'kind': 'required', 'name': '1 year as an RN'}]
    real = outcome.track
    outcome.track = lambda name: None   # take2-riley before matching loaded there (the owner's call, 6 Nov)
    try:
        short = outcome.short_items(crit, {}, {}, {'flags': ['license: no active New York RN license']})
    finally:
        outcome.track = real
    assert [c['id'] for c in short] == [82]
