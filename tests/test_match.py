"""Matching, the seeded jobs and what a screen says about a person, without a model or a database
(docs/BUILD-PLAN.md, "Matching" and "Properties and proof"). The properties run over generated people and the
twenty-six seeded jobs."""
import random

from hypothesis import given, settings
from hypothesis import strategies as st

from riley import match, seed
from riley.main import config
from riley.screener import instructions, job_config

JOBS = seed.demo_jobs()

people = st.fixed_dictionaries({}, optional={
    'licenses': st.lists(st.sampled_from(['NY RN', 'NY LPN', 'NY CNA']), unique=True).map(sorted),
    'years': st.floats(0, 20, allow_nan=False).map(lambda y: round(y, 1)),
    'certs': st.lists(st.sampled_from(sorted(match.CERTS)), unique=True).map(sorted),
    'units': st.lists(st.sampled_from(sorted(match.UNITS)), unique=True).map(sorted),
    'shifts': st.lists(st.sampled_from(sorted(match.SHIFTS)), unique=True).map(sorted),
})


def offered(facts: dict, jobs: list[dict]) -> set[int]:
    return {j['id'] for j in jobs if match.fit(facts, j, j['criteria'])['offer']}


def more(facts: dict, extra: dict) -> dict:
    """facts with everything in extra added: more items in each list, the larger number of years."""
    out = dict(facts)
    for k, v in extra.items():
        out[k] = max(out.get(k, 0), v) if k == 'years' else sorted({*out.get(k, []), *v})
    return out


# ---- properties ----

@given(people)
def test_offered_only_when_every_required_item_is_met(facts):
    for j in JOBS:
        f = match.fit(facts, j, j['criteria'])
        required = {c['name'] for c in j['criteria'] if c['kind'] == 'required'}
        if f['offer']:
            assert required <= set(f['met']) and not f['missing'] and not f['unknown'], (j['title'], f)
        else:
            assert f['missing'] or f['unknown']


@given(people, people)
def test_adding_a_fact_never_removes_a_match(facts, extra):
    assert offered(facts, JOBS) <= offered(more(facts, extra), JOBS)


@given(people, st.sampled_from(['BLS', 'ACLS', 'PALS', 'NRP']))
def test_removing_a_required_cert_removes_every_job_that_needs_it(facts, cert):
    without = {**facts, 'certs': [c for c in facts.get('certs', []) if c != cert]}
    needs = {j['id'] for j in JOBS for c in j['criteria'] if c['kind'] == 'required' and c['target'] == {'cert': cert}}
    assert needs and not offered(without, JOBS) & needs


@given(people)
def test_a_shift_they_cannot_work_is_never_offered(facts):
    for j in JOBS:
        if j['id'] in offered(facts, JOBS):
            assert match.shifts_of(j['shift']) & set(facts['shifts']), j['title']
    # Also for a job whose criteria forgot the shift: the job's own shift still counts
    bare = {**JOBS[0], 'criteria': [c for c in JOBS[0]['criteria'] if 'shift' not in c['target']]}
    if match.fit(facts, bare, bare['criteria'])['offer']:
        assert 'nights' in facts['shifts']


@given(st.floats(0, 10, allow_nan=False), st.floats(0, 10, allow_nan=False))
def test_partial_credit_grows_as_the_gap_closes(a, b):
    job = {'shift': None}
    crit = [{'kind': 'weighted', 'name': 'years', 'points': 10, 'target': {'years': 4}}]
    lo, hi = sorted([a, b])
    assert match.fit({'years': lo}, job, crit)['credit'] <= match.fit({'years': hi}, job, crit)['credit']


@given(people)
def test_suggestions_never_name_a_job_they_are_known_to_lack(facts):
    for j in match.suggest(facts, JOBS, limit=11):
        assert not match.fit(facts, j, j['criteria'])['missing']


# ---- metamorphic: changes that must not change any match ----

@settings(max_examples=50)
@given(people, st.randoms(use_true_random=False))
def test_renaming_reordering_or_adding_an_unreachable_job_changes_nothing(facts, rnd: random.Random):
    before = offered(facts, JOBS)
    fits = {j['id']: match.fit(facts, j, j['criteria']) for j in JOBS}
    renamed = [{**j, 'hospital': j['hospital'].replace('Hudson Park', 'Riverside')} for j in JOBS]
    shuffled = [{**j, 'criteria': rnd.sample(j['criteria'], len(j['criteria']))} for j in rnd.sample(JOBS, len(JOBS))]
    nobody = {'id': 99, 'title': 'RN, Hyperbaric', 'hospital': 'Hudson Park Midtown', 'shift': 'nights',
              'criteria': [{'kind': 'required', 'name': 'hyperbaric cert', 'target': {'cert': 'CHRN'}}]}
    for jobs in (renamed, shuffled, [*JOBS, nobody]):
        assert offered(facts, jobs) == before
    for j in shuffled:
        assert match.fit(facts, j, j['criteria']) == fits[j['id']]
    assert [j['id'] for j in match.suggest(facts, shuffled)] == [j['id'] for j in match.suggest(facts, JOBS)]


# ---- examples ----

def test_one_and_a_half_years_against_two_earns_three_quarters():
    crit = [{'kind': 'weighted', 'name': '2 years', 'points': 20, 'target': {'years': 2}}]
    assert match.fit({'years': 1.5}, {}, crit)['credit'] == 0.75


def test_the_happy_path_caller_fits_nights_jobs_with_acls():
    facts = {'licenses': ['NY RN'], 'years': 4.0, 'certs': ['ACLS', 'BLS'], 'units': ['med-surg', 'telemetry'],
             'shifts': ['nights']}
    titles = {j['title'] for j in JOBS if j['id'] in offered(facts, JOBS)}
    assert titles == {'RN, Medical-Surgical', 'RN, Telemetry'}, titles
    icu = next(j for j in JOBS if j['title'] == 'RN, ICU')
    assert match.fit(facts, icu, icu['criteria'])['missing'] == ['days']


def test_unknown_is_never_met():
    f = match.fit({}, JOBS[0], JOBS[0]['criteria'])
    assert not f['offer'] and not f['met'] and not f['missing'] and f['unknown']


def test_facts_from_a_screen():
    qs = config()['questions']
    answers = {'license': {'answer': 'yes, New York', 'meets': 'yes'},
               'years_rn': {'answer': 'about two and a half years', 'meets': 'yes'},
               'acute_care': {'answer': 'mostly tele, some med surg', 'meets': 'yes'},
               'certifications': {'answer': 'BLS is current but my ACLS lapsed', 'meets': 'yes'},
               'nights': {'answer': 'yes, nights work', 'meets': 'yes'},
               'other_roles': {'answer': 'sure', 'meets': 'yes'},
               'pay_expectation': {'answer': 'fifty five', 'meets': 'yes'}}
    assert match.facts_from(qs, answers) == {
        'licenses': ['NY RN'], 'years': 2.5, 'units': ['med-surg', 'telemetry'], 'certs': ['BLS'],
        'shifts': ['nights'], 'other_roles': True}
    # A "no" makes the item known and missing; an unconfirmed resume answer says nothing
    no = match.facts_from(qs, {'nights': {'answer': 'no', 'meets': 'no'},
                               'years_rn': {'answer': '6 years', 'meets': 'yes', 'confirmed': False}})
    assert no == {'shifts': []}


def test_labor_and_delivery_is_one_unit_and_negation_is_per_clause():
    assert match.found(match.UNITS, 'Two years in labor and delivery, and some peds') == ['l&d', 'peds']
    assert match.found(match.CERTS, "I have PALS and NRP, no TNCC") == ['NRP', 'PALS']


# ---- the seed ----

def test_seed_has_the_planned_sites_and_roles():
    hospitals, jobs = seed.jobs()
    assert len(hospitals) == 5 and len(jobs) == 26
    assert len({(j['title'], j['hospital']) for j in jobs}) == 26
    for j in jobs:
        ids = [q['id'] for q in j['questions']]
        assert len(ids) == len(set(ids)) and ids[-1] == 'other_roles', j['title']
        assert match.shifts_of(j['shift']), j['title']
        assert j['slots'] and j['facts'].get('pay'), j['title']
        for c in j['criteria']:
            assert c['kind'] in ('required', 'weighted', 'bonus') and len(c['target']) == 1, c
            if c['kind'] == 'required':
                assert c['reason'] and c['change'], (j['title'], c['name'])
            if 'cert' in c['target']:
                assert set([c['target']['cert']] if isinstance(c['target']['cert'], str) else c['target']['cert']) \
                    <= set(match.CERTS), c
            if 'unit' in c['target']:
                assert c['target']['unit'] in match.UNITS, c
        for q in j['questions']:
            assert q.get('fact') in (None, 'years', 'certs', 'units') or isinstance(q['fact'], dict), q


def test_the_med_surg_job_is_screen_yamls():
    cfg = config()
    medsurg = next(j for j in JOBS if j['title'] == cfg['agent']['job'])
    facts = {k: v for k, v in medsurg['facts'].items() if k != 'openings'}
    assert medsurg['questions'] == cfg['questions'] and facts == cfg['job_facts']


def test_a_chosen_job_brings_its_own_questions_and_facts():
    cfg = config()
    icu = job_config(cfg, next(j for j in JOBS if j['title'] == 'RN, ICU'))
    assert [q['id'] for q in icu['questions']][-3:] == ['start_date', 'pay_expectation', 'other_roles']
    text = instructions(icu)
    assert '[ccrn]' in text and '16-bed medical ICU' in text and '[nights]' not in text


def test_before_a_role_is_chosen_riley_lists_them_and_asks_nothing_else():
    text = instructions(config(), jobs=JOBS)
    assert '[2] RN, ICU at Hudson Park Midtown' in text and 'find_roles' in text and 'choose_role' in text
    assert '[license]' not in text and '$48' not in text
