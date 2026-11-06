"""Properties of the rules that already exist, over generated inputs (Hypothesis), without a model or a database.

Each test is one sentence that must hold for every input; the examples in test_store.py and test_prefill.py are single
cases of these. docs/BUILD-PLAN.md §"Properties and proof" names the rule each one proves.
"""
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given
from hypothesis import strategies as st

from riley import guest_call, helpline, outcomeline, seed, store
from riley.main import config
from riley.pro_speech import SPEAKS_SPANISH, ProSpeechTTS, stack_for
from riley.screener import (
    CAP,
    LONG,
    NO_LABELS,
    REASONS,
    SENTENCE_END,
    Screen,
    Screener,
    after_tool,
    fit,
    instructions,
    just_confirmed,
    roles_named,
    said_yes,
    same,
    says_back,
    still_to_ask,
    stray_roles,
    unsayable,
    years_meet,
)
from tests.test_outcome import line as message

CFG = config()
IDS = [q['id'] for q in CFG['questions']]
MEETS = st.sampled_from(['yes', 'no', 'unclear'])
# Any moment from 2020 to 2035, in UTC (daylight-saving changes in New York fall inside)
MOMENT = st.datetimes(min_value=datetime(2020, 1, 1), max_value=datetime(2035, 1, 1), timezones=st.just(UTC))
# Made-up values no template text contains, so finding one in the instructions means a record leaked
SECRET = st.from_regex(r'Zq[a-z]{8}', fullmatch=True)


def answers_of(meets: dict) -> dict:
    return {i: {'answer': 'x', 'meets': m, 'source': 'call', 'confirmed': True} for i, m in meets.items()}


ANSWERS = st.dictionaries(st.sampled_from(IDS), MEETS)


# ---- the scorecard ----

@given(ANSWERS, st.sampled_from(IDS))
def test_meeting_one_more_item_never_lowers_the_score(meets, qid):
    before = Screen(cfg=CFG, answers=answers_of(meets)).scorecard()['score']
    after = Screen(cfg=CFG, answers=answers_of({**meets, qid: 'yes'})).scorecard()['score']
    assert after >= before


@given(st.floats(min_value=0, max_value=45), st.floats(min_value=0, max_value=45))
def test_more_years_never_turns_a_yes_into_a_no(a, b):
    q = next(q for q in CFG['questions'] if q['id'] == 'years_rn')
    low, high = sorted((round(a, 1), round(b, 1)))
    if years_meet(q, f'{low} years') == 'yes':
        assert years_meet(q, f'{high} years') == 'yes'


@given(ANSWERS, st.dictionaries(st.sampled_from(IDS), MEETS))
def test_unconfirmed_answers_never_count(meets, resume):
    """Rule 5: anything still waiting for the caller's yes leaves the score where it was."""
    prefilled = {i: {'answer': 'from the resume', 'meets': m, 'source': 'resume'} for i, m in resume.items()}
    plain = Screen(cfg=CFG, answers=answers_of(meets)).scorecard()
    with_resume = Screen(cfg=CFG, answers=answers_of(meets), prefilled=prefilled).scorecard()
    assert with_resume['score'] == plain['score'] and with_resume['next_step'] == plain['next_step']
    assert set(with_resume['unconfirmed']) == set(prefilled) - set(meets)


# ---- the store's rules ----

TEN = st.from_regex(r'[2-9]\d{9}', fullmatch=True)


@given(st.text())
def test_keys_are_idempotent(raw):
    for key in (store.phone_key, store.email_key, store.license_key):
        assert key(key(raw)) == key(raw)


@given(TEN)
def test_two_spellings_of_one_number_find_one_person(n):
    spellings = [n, f'+1{n}', f'1{n}', f'({n[:3]}) {n[3:6]}-{n[6:]}', f'+1 {n[:3]}.{n[3:6]}.{n[6:]}',
                 f' {n[:3]} {n[3:]} ']
    assert {store.phone_key(s) for s in spellings} == {n}


@given(st.emails(), st.sampled_from([str.upper, str.lower, str.title]), st.sampled_from(['', ' ', '\t']))
def test_two_spellings_of_one_email_find_one_person(email, case, pad):
    assert store.email_key(pad + case(email) + pad) == store.email_key(email)


@given(MOMENT)
def test_everyone_has_a_promise_within_48_hours(applied):
    """Rule 2."""
    assert applied < store.promised_by(applied) <= applied + timedelta(hours=48)


@given(MOMENT, st.integers(min_value=1, max_value=24 * 30), st.integers(min_value=-24 * 60, max_value=24 * 60))
def test_no_decision_before_the_window_closes(first, window_hours, offset_hours):
    """Rule 3: can_decide is false at every moment before first_applied + window, true from then on."""
    now = first + timedelta(hours=offset_hours)
    job = {'first_applied': first, 'window_hours': window_hours}
    assert store.can_decide(job, now) == (now >= first + timedelta(hours=window_hours))


@given(MOMENT, st.timedeltas(min_value=timedelta(0), max_value=timedelta(days=30)))
def test_a_withdrawal_can_be_undone_only_within_7_days(withdrawn, after):
    assert store.can_undo(withdrawn, withdrawn + after) == (after <= timedelta(days=7))


# ---- the help line ----

MONTH_DAY = re.compile(r'\b(January|February|March|April|May|June|July|August|September|October|November|December) '
                       r'(\d{1,2})\b')
ZONES = st.sampled_from(['America/New_York', 'America/Chicago', 'America/Los_Angeles', 'Pacific/Honolulu'])
ROW = st.fixed_dictionaries({
    'id': st.integers(1, 10_000), 'title': st.sampled_from(['RN, ICU', 'RN, telemetry', 'CNA']),
    'location': st.sampled_from(['Midtown', 'Bronx', 'Brooklyn']), 'applied': MOMENT,
    'promised_by': st.one_of(st.none(), MOMENT), 'applicants': st.integers(0, 500),
    'status': st.sampled_from([*helpline.STATUS, 'unknown_status'])})


@given(st.lists(ROW, max_size=4), ZONES)
def test_help_line_never_says_a_date_not_in_the_row(rows, tz):
    """Every date the help line reads out is the applied or promised date of one of the caller's own rows."""
    said = {m.group(0) for m in MONTH_DAY.finditer(helpline.lines(rows, 'Dana', tz))}
    own = set()
    for r in rows:
        for d in (r['applied'], r['promised_by']):
            if d:
                local = d.astimezone(ZoneInfo(tz))
                own.add(f"{local.strftime('%B')} {local.day}")
    assert said <= own


@given(st.one_of(st.none(), MOMENT), ZONES)
def test_promised_time_names_only_that_day(dt, tz):
    text = helpline.when(dt, tz)
    if dt is None:
        assert text == 'no date yet'
    else:
        local = dt.astimezone(ZoneInfo(tz))
        assert [m.group(0) for m in MONTH_DAY.finditer(text)] == [f"{local.strftime('%B')} {local.day}"]


# ---- identity ----

@given(st.dictionaries(st.sampled_from(IDS), SECRET, min_size=1), st.lists(ROW, min_size=1, max_size=3), SECRET)
def test_before_a_yes_the_instructions_hold_none_of_the_callers_records(resume, rows, place):
    """Whatever their resume and applications say, none of it is in what the model is told until they confirm."""
    who = {'name': 'John Doe', 'title': 'RN, ICU', 'applied': datetime(2026, 11, 6, 15, tzinfo=UTC)}
    prefilled = {i: {'answer': v, 'meets': 'yes', 'source': 'resume'} for i, v in resume.items()}
    rows = [{**r, 'location': place} for r in rows]
    screen = Screener(CFG, prefilled=prefilled, who=who).instructions
    help_ = helpline.HelpLine(CFG, rows, who=who).instructions
    for secret in [*resume.values(), place]:
        assert secret not in screen and secret not in help_
    # and once they confirm, the resume is what the model gets
    assert all(v in instructions(CFG, prefilled=prefilled) for v in resume.values())


# ---- what Riley may say (hard callers, 6 Nov: tracks 5, 14 and 4) ----

JOBS = seed.demo_jobs()
JOB_IDS = [j['id'] for j in JOBS]
WORDS = st.text(st.sampled_from('abcdefghij '), max_size=30)


@given(st.lists(st.sampled_from(IDS), unique=True))
def test_no_part_of_record_answers_result_is_ever_spoken(left):
    """Whatever is left to ask, every sentence of the tool result is dropped by the speech filter."""
    note = still_to_ask(left)
    assert all(unsayable(s) for s in SENTENCE_END.split(note) if s.strip()), note


@given(WORDS, st.sampled_from([i for i in IDS if '_' in i]), WORDS)
def test_a_sentence_with_a_question_id_is_never_spoken(before, qid, after):
    assert unsayable(f'{before} {qid} {after}.') == 'tool words'


@pytest.mark.parametrize('line', ['I understand how frustrating this is.', 'I understand how you feel.',
                                  'I know how hard this must be.', 'I can imagine how upsetting that is.',
                                  'Please calm down.', 'Entiendo lo frustrante que es.'])
def test_lines_that_claim_to_know_a_feeling_are_never_spoken(line):
    assert unsayable(line) == 'labels a feeling'


@pytest.mark.parametrize('line', ['Let me continue in Spanish.', "I'll continue in English.",
                                  'Switching to Spanish now.', "So I'll confirm that now.",
                                  "Yes, you're the person on file and this is about the role."])
def test_saying_the_language_switch_is_never_spoken(line):
    # "Let me continue in Spanish." came out in English on a Spanish guest call (hard callers, 6 Nov)
    assert unsayable(line) == 'narration'


@pytest.mark.parametrize('line', ["That's hard to hear.", "It's fair to ask.", 'Gracias. ¿Cuántos años lleva como '
                                  'enfermera?', 'How many years have you worked as an RN?', 'I hear you.'])
def test_plain_lines_are_spoken(line):
    assert unsayable(line) == ''


def test_each_open_role_is_named_by_its_own_title_and_no_other():
    for j in JOBS:
        assert roles_named(f"There's the {j['title']} job at {j['hospital']}.", JOBS) == {j['id']}, j['title']


@given(st.sets(st.sampled_from(JOB_IDS)), st.lists(st.sampled_from(JOB_IDS), min_size=1, max_size=11))
def test_before_a_pick_riley_names_only_roles_find_roles_returned_or_the_caller_named(allowed, said):
    """A sentence naming roles is spoken only when every one is allowed; a list of all eleven never is."""
    titles = {j['id']: j['title'].split(', ')[-1] for j in JOBS}
    sentence = 'We have ' + ', '.join(titles[i] for i in said) + '.'
    assert stray_roles(sentence, JOBS, allowed) == set(said) - allowed
    assert not stray_roles(sentence, JOBS, set(said))


@given(st.lists(st.text(st.sampled_from('ab. ?¿'), max_size=20), max_size=4))
def test_after_record_answer_a_turn_without_a_question_still_gets_one(said):
    """The second pass is silent once this turn asked something; otherwise it may say only the next question."""
    assert after_tool(said) == ('silent' if any('?' in t for t in said) else 'question')


@given(st.sampled_from(['english-fast', 'multilingual', 'lfm3-audio', 'whisper-pocket']))
def test_spanish_always_gets_a_pro_stack_that_speaks_it_and_english_keeps_the_configured_one(stack):
    assert stack_for(stack, 'es') in SPEAKS_SPANISH and stack_for(stack, 'en') == stack
    voice = ProSpeechTTS('http://pro', stack=stack)
    voice.switch('es')
    assert (voice.stack, voice.lang) == (stack_for(stack, 'es'), 'es')
    voice.switch('en')
    assert (voice.stack, voice.lang) == (stack, 'en')


async def test_every_screen_can_switch_to_spanish_and_keeps_it_when_the_role_is_chosen():
    agent = Screener(CFG, jobs=JOBS)
    assert {'set_language', 'list_roles', 'find_roles'} <= {t.info.name for t in agent.tools}
    tool = next(t for t in agent.tools if t.info.name == 'set_language')
    state = Screen(cfg=CFG)
    await tool(SimpleNamespace(userdata=state), 'es')
    assert state.language == 'es' and agent.instructions.endswith(guest_call.SPANISH)
    await agent.update_instructions(instructions(CFG))   # what choose_role does
    assert agent.instructions.endswith(guest_call.SPANISH)
    assert agent.instructions.count(guest_call.SPANISH) == 1


def test_the_role_list_is_for_matching_only_and_the_outcome_call_shares_the_feelings_rule():
    assert 'never read it out' in instructions(CFG, jobs=JOBS) and 'list_roles' in instructions(CFG, jobs=JOBS)
    _agent, state = message('not_yet')
    said = outcomeline.instructions(CFG, state.message, 'en')
    assert NO_LABELS in said and NO_LABELS in instructions(CFG)


# ---- the outcome call's whole-reply cut (screener.fit) ----
WORD = st.from_regex(r'[a-z]{1,8}', fullmatch=True)
SENT = st.tuples(st.lists(WORD, min_size=1, max_size=25), st.booleans()).map(
    lambda t: ' '.join(t[0]).capitalize() + ('?' if t[1] else '.'))
REPLY = st.lists(SENT, min_size=1, max_size=8, unique_by=same)


def _n(xs):
    return sum(len(x.split()) for x in xs)


@given(REPLY, st.integers(0, 40), st.data())
def test_an_outcome_reply_stays_under_60_words_and_lines_in_must_say_still_win(reply, before, data):
    must_said = data.draw(st.lists(st.sampled_from(reply), max_size=2, unique=True))
    must = [same(x) for x in must_said]
    kept = fit(reply, must, before)
    # A sentence of a must_say line counts as that line (the model may split it), as in the speech filter
    required = [s for s in reply if any(same(s) in m for m in must)]
    # Only what the model wrote, every required line kept; in its order, except that the reply ends on its question
    assert kept == sorted([s for s in reply if s in kept], key=lambda s: s.endswith('?'))
    assert all(s in kept for s in required)
    # Never over: only the required lines may take the reply to 60 words or past it
    extra = [s for s in kept if s not in required]
    if extra:
        assert before + _n(kept) < CAP
    # One question at most besides the required lines, none when a required line asks, and nothing after it
    asked = [i for i, s in enumerate(kept) if s.endswith('?') and s not in required]
    assert len(asked) <= 1
    if any(s.endswith('?') for s in required):
        assert not asked
    if asked:
        assert all(s in required and s.endswith('?') for s in kept[asked[0] + 1:])


@given(REPLY, st.data())
def test_a_reply_ends_on_its_question_with_the_update_before_it(reply, data):
    must_said = data.draw(st.lists(st.sampled_from(reply), max_size=2, unique=True))
    kept = fit(reply, [same(x) for x in must_said])
    # Every statement comes before every question, so an update is never said after the offer it leads to
    first_q = next((i for i, s in enumerate(kept) if s.endswith('?')), len(kept))
    assert not any(s.endswith('?') for s in kept[:first_q]) and all(s.endswith('?') for s in kept[first_q:])
    assert all(s in kept for s in must_said)


def test_the_no_show_reply_gives_the_update_then_one_offer():
    # The reply from the no_show run (6 Nov), less the sentence that said back their reason (says_back)
    update = ['We missed you at your second interview for the RN, Medical-Surgical role on Thursday, November 5.',
              'You have one more chance: would you like Dana to set a new time?']
    reply = ["Since you've confirmed it's you and the role, I can help with that.",
             'Would you like to be offered a new interview time?', *update]
    kept = fit(reply, [same(x) for x in update])
    assert kept[-2:] == update and 'Would you like to be offered a new interview time?' not in kept
    assert _n(kept) < CAP


# ---- a follow-up never says back the reason they gave (screener.says_back) ----
REASON_WORD = st.sampled_from(['came up', 'home', 'family', 'kids', 'my son', 'sick', 'the hospital', 'a funeral',
                               'my car', 'traffic', 'my mom', 'a fever', 'something happened'])
FILLER = st.lists(WORD.filter(lambda w: not any(re.search(rf'\b({p})\b', w, re.I) for p in REASONS.values())),
                  max_size=8).map(' '.join)


@given(REASON_WORD, FILLER, FILLER, FILLER)
def test_a_follow_up_never_says_back_the_reason_they_gave(reason, a, b, c):
    heard = f'{a} sorry, {reason} {b}'
    assert says_back(f"I'm sorry {c} {reason}.", heard)
    assert says_back(f'{reason.capitalize()} {c}, I hope all is well.', heard)
    # The employer's name and a line code wrote for the call never count, whatever they said
    assert not says_back('This is Riley from Hudson Park Health.', 'my health was bad', names=['Hudson Park Health'])


@given(REASON_WORD, FILLER)
def test_a_sentence_without_their_reason_words_is_kept(reason, c):
    assert not says_back(f'Would you like Dana to set a new time {c}?', f'sorry, {reason}')
    assert not says_back(f'Thanks for letting me know {c}.', f'sorry, {reason}')
    # A word they never said is not theirs to have said back
    assert not says_back('Is now still a good time?', 'it was traffic')


def test_the_reply_after_the_identity_yes_never_ends_the_call():
    assert just_confirmed(['confirm_identity'], 'confirmed')
    # A "not them" answer must end the call, and a later reply may end it
    assert not just_confirmed(['confirm_identity'], 'not_them') and not just_confirmed(['new_time'], 'confirmed')
    # The yes and end_call in one generation (follow_up_not_yet, 6 Nov): the tool has not run yet
    assert just_confirmed(['confirm_identity'], 'pending', True)
    assert said_yes('{"is_them": true}') and not said_yes('{"is_them": false}') and not said_yes('not json')


def test_saying_the_identity_check_aloud_is_narration():
    line = ("Before I go further, I need to confirm you're the applicant and that this is about the role you applied "
            "for, so I'm calling the identity check now.")
    assert unsayable(line) == 'narration'
    assert not unsayable('We missed you at your second interview for the RN, Medical-Surgical role.')


def test_the_no_show_run_sentence_is_said_back():
    heard = 'Yes, this is Ana. Sorry, something came up at home.'
    assert says_back("Thanks, Ana, I'm sorry you had something come up at home.", heard)
    assert not says_back('Would you like to be offered a new interview time?', heard)


@given(REPLY, st.integers(0, 40))
def test_a_question_that_fits_is_kept_by_dropping_what_came_before_it(reply, before):
    kept = fit(reply, [], before)
    q = next((s for s in reply if s.endswith('?')), None)
    if q is not None and before + len(q.split()) < CAP:
        assert q in kept
    if q is None or q not in kept:
        # No question kept: what is said is a lead of the reply within the 45-word turn
        assert _n(kept) + before <= LONG or not kept
    lead = [s for s in kept if not s.endswith('?')]
    assert lead == reply[:len(lead)]   # the last sentences before the question go first


def test_the_62_word_outcome_reply_is_cut_under_60_and_keeps_its_question():
    reply = ["That's hard to hear, and it's fair to ask.",
             'The reason is that your BLS lapsed, and renewing it is what would change this.',
             'Dana can look at it again with you, and there is a telemetry role that fits what you have now.',
             'You can also give feedback and Dana reads it in your words.',
             'Would you like to be considered for the telemetry role?']
    assert _n(reply) > 60
    kept = fit(reply, [])
    assert _n(kept) < 60 and kept[-1] == reply[-1] and kept[0] == reply[0]


REMINDER = ('A reminder: your second interview for the RN, Medical-Surgical role is Tuesday, November 10, at 10 am, '
            'with a nurse interviewer from the unit; can you still make it?')


def test_a_cut_reminder_is_said_whole():
    from riley.screener import same, whole_line
    assert whole_line('Can you still make it? ', [REMINDER], set()) == REMINDER + ' '
    # once said, the fragment stays a fragment (and the repeat check drops it)
    assert whole_line('Can you still make it? ', [REMINDER], {same(REMINDER)}) == 'Can you still make it? '
    # a short or unrelated sentence is left alone
    assert whole_line('Make it? ', [REMINDER], set()) == 'Make it? '
    assert whole_line('Thanks for letting me know. ', [REMINDER], set()) == 'Thanks for letting me know. '


@given(st.text(alphabet='abcdefgh ', min_size=0, max_size=40))
def test_a_line_said_inside_a_longer_one_is_a_repeat(words):
    from riley.screener import repeats, same
    longer = same(f"I'll pass that along, and Dana will call you back {words}.")
    assert repeats(same('Dana will call you back.'), {longer})
    assert not repeats(same('Thanks.'), {same('Thanks for that, Ana.')})
