"""Resume answers and a second role, without a model or a database (SPEC §3)."""
from riley import store
from riley.main import config
from riley.screener import Screen, carry_over, confirm, instructions, settled

RESUME = {'years_rn': {'answer': '4 years', 'meets': 'yes', 'source': 'resume'}}


def test_unconfirmed_resume_answer_is_not_scored():
    s = Screen(cfg=config(), prefilled=RESUME)
    card = s.scorecard()
    assert card['score'] == 0 and 'years_rn' in card['missing'] and card['unconfirmed'] == ['years_rn']


def test_confirmed_resume_answer_counts():
    s = Screen(cfg=config(), prefilled=RESUME)
    s.answers['years_rn'] = confirm(RESUME['years_rn'], 12)
    card = s.scorecard()
    assert card['score'] > 0 and 'years_rn' not in card['missing'] and not card['unconfirmed']
    assert s.answers['years_rn']['source'] == 'resume' and s.answers['years_rn']['confirmed']


def test_second_role_keeps_person_answers_and_asks_role_ones():
    cfg = config()
    done = {i: {'answer': '3 years', 'meets': 'yes', 'source': 'call'} for i in
            ('license', 'years_rn', 'nights', 'start_date', 'pay_expectation')}
    kept = carry_over(cfg, done)
    assert set(kept) == {'license', 'years_rn'} and not any(v['recheck'] for v in kept.values())
    text = instructions(cfg, carried=kept)
    assert 'never ask these again' in text
    assert '[years_rn] How many years' not in text and '[nights]' in text and '[start_date]' in text


def test_resume_read_back_is_in_the_instructions():
    text = instructions(config(), prefilled=RESUME)
    assert 'From their resume' in text and 'confirm_resume' in text and '[years_rn] 4 years' in text
    assert 'From their resume' not in instructions(config())


def test_call_answers_split_resume_from_earlier_roles():
    apps = [{'id': 1, 'answers': {'years_rn': {'value': '5 years', 'source': 'call', 'confirmed': True, 'meets': 'yes'},
                                  'nights': {'value': 'yes', 'source': 'call', 'confirmed': True}}},
            {'id': 2, 'answers': {'certifications': {'value': 'BLS', 'source': 'resume', 'confirmed': False},
                                  'years_rn': {'value': '4 years', 'source': 'resume', 'confirmed': True}}}]
    resume, earlier = store.call_answers(apps, 2)
    assert resume == {'certifications': {'answer': 'BLS', 'meets': 'unclear', 'source': 'resume'}}
    assert set(earlier) == {'years_rn', 'nights'} and earlier['years_rn']['answer'] == '5 years'


def test_stored_shape_round_trips():
    s = {'years_rn': confirm(RESUME['years_rn'], 3)}
    back = store.stored_answers(s)
    assert back == {'years_rn': {'value': '4 years', 'meets': 'yes', 'source': 'resume', 'confirmed': True}}


def test_another_department_rechecks_experience_against_its_own_needs():
    """Telemetry met a telemetry job; for an ICU job Riley asks whether it covers what the ICU needs (6 Nov)."""
    cfg = config()
    icu = {**cfg, 'questions': [{**q, 'good': 'at least 2'} if q['id'] == 'years_rn' else
                                {**q, 'good': 'ICU or other critical care'} if q['id'] == 'acute_care' else q
                                for q in cfg['questions']]}
    earlier = {'license': {'answer': 'yes, NY', 'meets': 'yes'},
               'years_rn': {'answer': '18 months', 'meets': 'yes'},          # met "at least 1", not "at least 2"
               'acute_care': {'answer': 'telemetry', 'meets': 'yes'}}
    kept = carry_over(icu, earlier)
    assert kept['license'] == {**earlier['license'], 'source': 'earlier screen', 'meets': 'yes', 'recheck': False}
    assert kept['years_rn']['meets'] == 'no' and not kept['years_rn']['recheck']
    assert kept['acute_care']['recheck'] and kept['acute_care']['meets'] == 'unclear'
    assert set(settled(kept)) == {'license', 'years_rn'}
    text = instructions(icu, carried=kept)
    assert ('"telemetry"' in text and 'this role needs: ICU or other critical care' in text
            and '[license] Do you hold' not in text)
