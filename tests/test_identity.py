"""The identity check, without a model or a database: a known number hears nothing about itself until the caller
confirms the name and role."""
from datetime import UTC, datetime

import pytest
from livekit.agents.llm import ToolError

from riley import helpline, identity
from riley.main import config
from riley.screener import Screen, Screener
from tests.test_helpline import ROWS
from tests.test_prefill import RESUME

WHO = {'name': 'John Doe', 'title': 'night-shift med-surg nurse', 'applied': datetime(2026, 11, 6, 15, tzinfo=UTC)}


def test_question_names_the_person_and_the_role():
    q = identity.question(WHO)
    assert q == 'Am I speaking with John Doe, about the night-shift med-surg nurse role you applied for on Friday, ' \
                'November 6?'
    hello = identity.greeting(config(), WHO)
    assert 'AI assistant' in hello and hello.endswith(q)


def test_help_line_holds_no_status_until_confirmed():
    hidden = helpline.instructions(config(), ROWS, hidden=True)
    for leak in ('Midtown', 'Bronx', 'November', 'applicants', '[7]'):
        assert leak not in hidden, leak
    agent = helpline.HelpLine(config(), ROWS, who=WHO)
    assert 'confirm_identity' in {t.info.name for t in agent.tools}
    assert 'Midtown' not in agent.instructions


def test_screener_holds_no_resume_until_confirmed():
    agent = Screener(config(), prefilled=RESUME, who=WHO)
    assert 'From their resume' not in agent.instructions and 'confirm_identity' in agent.instructions
    assert 'confirm_identity' not in {t.info.name for t in Screener(config()).tools}


def test_tools_refuse_before_confirmation():
    state = Screen(cfg=config(), identity='pending')
    with pytest.raises(ToolError):
        identity.check(state)
    for done in ('none', 'confirmed'):
        identity.check(Screen(cfg=config(), identity=done))


def _openings(cfg: dict) -> dict:
    """The first line of every call Riley places, one per outbound mode (riley/outbound.py MODES)."""
    from riley import followup, guest_call, outbound, outcomeline, scheduler
    from tests.test_outcome import line as message
    items = [{'kind': 'no_show_follow_up', 'event_id': 1, 'candidate_id': 1, 'application_id': 2,
              'detail': {'title': 'RN, Telemetry', 'at': '2026-11-05T15:00:00+00:00', 'paused': False}}]
    follow = followup.FollowUp(cfg, items, scheduler.lines(items, 'Dana'))
    _agent, state = message('not_yet')
    out = {
        'guest': [Screener(cfg, demo={'name': 'Jordan Lee', 'title': 'RN, ICU', 'hospital': 'Hudson Park Midtown'},
                           outbound=True).opening()],
        'outcome': [outcomeline.OutcomeLine(cfg, state, WHO, placed=True).opening()],
        'status': [guest_call.status_line(cfg)],
        # A callback is a screen, or the help line for someone with nothing left to screen
        'callback': [Screener(cfg, placed='callback').opening(), Screener(cfg, who=WHO, placed='callback').opening(),
                     helpline.HelpLine(cfg, ROWS, who=WHO, placed='callback').opening()],
        'follow_up': [followup.FollowUpLine(cfg, follow, WHO).opening()],
        # the first screen of someone a recruiter imported (web/console.py api_screen)
        'screen': [Screener(cfg, placed='screen').opening(), Screener(cfg, who=WHO, placed='screen').opening()],
    }
    assert set(out) == set(outbound.MODES)
    return out


WHY = {'guest': 'calling for your demo', 'outcome': 'calling with an update on your application',
       'status': 'screen is done', 'callback': 'calling you back, as you asked',
       'follow_up': 'calling with an update on your application',
       'screen': 'calling about the nursing role you applied for'}


@pytest.mark.parametrize('notice', [True, False])
def test_a_call_riley_places_says_who_is_calling_from_where_and_why_never_thanks_for_calling(notice):
    cfg = config()
    cfg = {**cfg, 'rules': {**cfg['rules'], 'recording_notice': notice}}
    a = cfg['agent']
    for mode, lines in _openings(cfg).items():
        for hello in lines:
            first = hello.split('.')[0] + '.'
            assert 'thanks for calling' not in hello.lower(), (mode, hello)
            assert a['name'] in first and a['employer'] in hello, (mode, hello)
            assert WHY[mode] in hello, (mode, hello)


def test_a_call_they_make_still_opens_with_thanks_for_calling():
    from riley.screener import greeting
    from tests.test_outcome import line as message
    cfg = config()
    agent, _state = message('not_yet')
    for hello in (greeting(cfg), Screener(cfg, who=WHO).opening(), helpline.HelpLine(cfg, ROWS, who=WHO).opening(),
                  agent.opening()):
        assert hello and hello.startswith(f"Hi, thanks for calling {cfg['agent']['employer']}")


# ---- an email for a phone applicant (docs/decisions/0027-apply-page.md) ----

def test_riley_asks_a_caller_with_no_email_for_one_and_never_a_guest():
    from riley.screener import contact_note, spelled
    cfg = config()
    note = contact_note(cfg['agent'])
    assert note.strip() in Screener(cfg, ask_email=True).instructions
    assert note.strip() not in Screener(cfg).instructions
    assert note.strip() not in Screener(cfg, ask_email=True, demo={'name': 'Jordan Lee', 'title': 'RN, ICU',
                                                                     'hospital': 'Hudson Park Midtown'}).instructions
    assert spelled('rosa.diaz-2@gmail.com') == 'R O S A dot D I A Z dash 2, at gmail dot com'


async def test_save_contact_keeps_a_whole_address_or_a_text_instead():
    from types import SimpleNamespace
    cfg = config()
    agent = Screener(cfg, ask_email=True)
    tool = next(t for t in agent.tools if t.info.name == 'save_contact')
    state = Screen(cfg=cfg)
    with pytest.raises(ToolError):
        await tool(SimpleNamespace(userdata=state), 'rosa at gmail')
    assert state.contact == {}
    said = await tool(SimpleNamespace(userdata=state), ' Rosa.Diaz@Gmail.com ')
    assert state.contact == {'email': 'rosa.diaz@gmail.com'} and 'R O S A dot D I A Z, at gmail dot com' in said
    await tool(SimpleNamespace(userdata=state), '', True)
    assert state.contact == {'texts': True}
