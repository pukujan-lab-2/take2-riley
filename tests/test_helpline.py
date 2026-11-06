"""The help line, without a model or a database: what it says, and that it never touches answers or scores."""
import asyncio
from datetime import UTC, datetime

from riley import helpline, octo
from riley.main import config

ROWS = [{'id': 7, 'candidate_id': 3, 'timezone': 'America/New_York', 'title': 'RN, night shift', 'location': 'Midtown',
         'applied': '2026-11-03T14:00:00+00:00', 'promised_by': '2026-11-05T20:00:00+00:00', 'status': 'screened',
         'withdrawn': None, 'applicants': 41},
        {'id': 9, 'candidate_id': 3, 'timezone': 'America/New_York', 'title': 'RN, telemetry', 'location': 'Bronx',
         'applied': '2026-11-04T15:30:00+00:00', 'promised_by': '2026-11-06T15:30:00+00:00', 'status': 'applied',
         'withdrawn': None, 'applicants': 12}]


def test_promised_time_is_said_like_a_person():
    assert helpline.when(datetime(2026, 11, 5, 20, 0, tzinfo=UTC)) == 'Thursday, November 5, by 3 pm'
    assert helpline.when(datetime(2026, 11, 6, 15, 30, tzinfo=UTC)) == 'Friday, November 6, by 10:30 am'
    assert helpline.when(None) == 'no date yet'


def test_status_lines_carry_each_application_and_nothing_else():
    text = helpline.lines(ROWS, 'Dana')
    assert 'applied Tuesday, November 3' in text and 'hear by Thursday, November 5, by 3 pm' in text
    assert "Dana's decision" in text and 'applicants for this job so far: 12' in text
    assert text.count('\n- ') == 1 and '[7]' in text and '[9]' in text


def test_withdrawn_has_no_date():
    text = helpline.lines([{**ROWS[0], 'status': 'withdrawn'}], 'Dana')
    assert 'no date: withdrawn' in text


def test_help_line_cannot_record_answers():
    agent = helpline.HelpLine(config(), ROWS)
    names = {t.info.name for t in agent.tools}
    assert 'record_answer' not in names and 'confirm_resume' not in names
    assert {'fix_fact', 'withdraw', 'undo_withdraw', 'give_feedback'} <= names


def test_saving_a_help_call_never_writes_answers_or_score(monkeypatch):
    sql = []

    async def query(q, params=None):
        sql.append(q)
        return [{'withdrawn': datetime.now(UTC)}]
    monkeypatch.setattr(octo, 'query', query)
    h = helpline.Help(config(), ROWS)
    h.requests = [{'kind': 'fix', 'application_id': 7,
                   'detail': {'item': 'certifications', 'now_says': 'ACLS current'}},
                  {'kind': 'feedback', 'application_id': None, 'detail': {'words': 'quick', 'share': True}}]
    h.withdraw, h.undo = [9], [9]
    asyncio.run(helpline.save('live', 3, h))
    assert sum('insert into events' in q for q in sql) == 3
    assert not any('answers' in q or 'score =' in q for q in sql), sql


def test_asking_for_another_role_on_the_help_line_is_their_yes_and_never_a_closed_door(monkeypatch):
    text = helpline.instructions(config(), ROWS)
    assert "Never say they can't be considered for another position" in text
    assert 'renewal date or the new card' in text
    sql = []

    async def query(q, params=None):
        sql.append((q, params))
        return [{'facts': {}}]
    monkeypatch.setattr(octo, 'query', query)
    h = helpline.Help(config(), ROWS)
    h.requests = [{'kind': 'add_role', 'application_id': None, 'detail': {'role': 'PCT for med-surg ICU'}}]
    asyncio.run(helpline.save('live', 3, h))
    assert any('consent = consent ||' in q and '"other_roles": true' in p[2] for q, p in sql)
