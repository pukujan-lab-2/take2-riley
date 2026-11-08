"""Intake properties (docs/BUILD-PLAN.md, "Properties and proof", track 9), without a model or a database."""
import re
from datetime import UTC, datetime
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from riley import store
from intake import extract, jobpost, load

FIXTURES = Path(__file__).resolve().parents[1] / 'samples'
RESUME = (FIXTURES / 'ana_morales.txt').read_text()
POST = (FIXTURES / 'job_post.txt').read_text()
QUESTIONS = load.config()['questions']

SPELLINGS = ['({a}) {b}-{c}', '{a}-{b}-{c}', '{a}.{b}.{c}', '{a} {b} {c}', '+1 {a} {b} {c}', '+1 ({a}) {b}-{c}',
             '1-{a}-{b}-{c}', '{a}{b}{c}', '+1{a}{b}{c}', '1 ({a}) {b} {c}']
digits = st.text('0123456789', min_size=1, max_size=1)
area = st.tuples(st.sampled_from('23456789'), digits, digits).map(''.join)
three, four = st.text('0123456789', min_size=3, max_size=3), st.text('0123456789', min_size=4, max_size=4)


@given(area, three, four, st.sampled_from(SPELLINGS), st.sampled_from(SPELLINGS))
def test_a_phone_in_any_spelling_extracts_to_one_key(a, b, c, one, other):
    texts = [f'Ana Morales\nPhone: {s.format(a=a, b=b, c=c)}\nBLS, ACLS\n' for s in (one, other)]
    keys = [extract.facts(t)['phone'] for t in texts]
    assert keys[0] == keys[1] == a + b + c == store.phone_key(one.format(a=a, b=b, c=c))


def all_formats() -> list[dict]:
    out = []
    for name in ('ana_morales.txt', 'ana_morales.pdf', 'ana_morales.docx', 'application.eml', 'board_export.csv'):
        out += extract.items(name, (FIXTURES / name).read_bytes())[:1]   # the CSV's first row is Ana
    return [*out, extract.pasted(RESUME)]


def test_the_same_resume_gives_the_same_facts_in_every_format():
    facts = [extract.resolve(i) for i in all_formats()]
    assert facts[0] == {'name': 'Ana Morales', 'phone': '7185550142', 'email': 'ana.morales@example.com',
                        'license': 'NY-654321', 'license_number': '654321', 'license_state': 'NY',
                        'license_type': 'RN', 'years': 4, 'units': ['med-surg', 'telemetry'],
                        'certifications': ['ACLS', 'BLS'], 'languages': ['English', 'Spanish']}
    assert all(f == facts[0] for f in facts), [i for i, f in enumerate(facts) if f != facts[0]]
    assert [i['source'] for i in all_formats()] == ['upload', 'upload', 'upload', 'email', 'csv', 'paste']


def test_a_board_csv_is_one_candidate_per_row_and_the_job_column_is_not_experience():
    rows = extract.items('board_export.csv', (FIXTURES / 'board_export.csv').read_bytes())
    sam = extract.resolve(rows[1])
    assert len(rows) == 2 and sam['name'] == 'Sam Lee' and sam['email'] == 'sam.lee@example.com'
    assert sam['phone'] == '2125550177' and sam['license'] == 'NJ-7654321' and sam['units'] == ['icu']


def test_a_forwarded_email_never_takes_the_recruiter_as_the_candidate():
    body, files, sender = extract.email_parts((FIXTURES / 'application.eml').read_bytes())
    assert sender == {} and len(files) == 1 and 'resume attached' in body


resume_parts = st.fixed_dictionaries({
    'years': st.integers(1, 30), 'state': st.sampled_from(['New York', 'NY', 'New Jersey', 'PA']),
    'number': st.integers(10000, 9999999), 'certs': st.sets(st.sampled_from(['BLS', 'ACLS', 'PALS', 'NRP'])),
    'units': st.sets(st.sampled_from(['Med-Surg', 'Telemetry', 'ICU', 'Emergency Department', 'Rehab']))})


def resume_from(p: dict) -> str:
    return (f"Pat Rivera\n212-555-0199\nLicense: {p['state']} RN {p['number']}\n{p['years']} years as an RN on "
            f"{', '.join(sorted(p['units'])) or 'a clinic'}\nCertifications: {', '.join(sorted(p['certs']))}\n")


@given(resume_parts)
def test_nothing_extracted_is_ever_confirmed(p):
    f = extract.facts(resume_from(p))
    answers = extract.prefill(f, QUESTIONS)
    assert f['years'] == p['years'] and set(f.get('certifications', [])) >= p['certs']
    assert answers and all(v['source'] == 'resume' and v['confirmed'] is False for v in answers.values())
    assert set(answers) <= {q['id'] for q in QUESTIONS}


answer = st.fixed_dictionaries({'value': st.text(max_size=8), 'meets': st.sampled_from(['yes', 'no', 'unclear']),
                                'source': st.sampled_from(['resume', 'call', 'earlier screen']),
                                'confirmed': st.booleans()})
ids = st.sampled_from(['license', 'years_rn', 'acute_care', 'certifications', 'nights'])


@given(st.dictionaries(ids, answer), resume_parts)
def test_a_resume_never_overwrites_what_the_person_confirmed(existing, p):
    new = extract.prefill(extract.facts(resume_from(p)), QUESTIONS)
    merged = extract.merge_prefill(existing, new)
    for k, v in existing.items():
        if v['confirmed'] or v['source'] != 'resume':
            assert merged[k] == v
    assert all(not v['confirmed'] for k, v in merged.items() if k not in existing or merged[k] != existing[k])
    assert set(merged) == set(existing) | set(new)


class FakeStore:
    """Just enough of riley/store.py to watch what an import writes."""

    def __init__(self):
        self.people, self.apps, self.resumes, self.writes = {}, {}, [], 0

    async def upsert_candidate(self, sandbox, name=None, phone=None, email=None, license=None):
        key = store.license_key(license) or store.phone_key(phone) or store.email_key(email)
        return self.people.setdefault(key, {'id': len(self.people) + 1, 'name': name})

    async def apply(self, sandbox, candidate_id, job_id, resume=None, facts=None, source='upload', now=None):
        self.writes += 1
        old = self.apps.get((candidate_id, job_id))
        app = old or {'id': len(self.apps) + 1, 'applied': datetime(2026, 11, 2, tzinfo=UTC), 'answers': {}}
        self.apps[(candidate_id, job_id)] = app
        self.resumes.append((candidate_id, job_id, resume))
        return {**app, 'reapplied': bool(old), 'changes': [], 'conflicts': []}

    async def known_resume(self, candidate_id, job_id, text):
        return (candidate_id, job_id, text) in self.resumes

    async def save_prefill(self, application_id, answers):
        self.writes += 1
        next(a for a in self.apps.values() if a['id'] == application_id)['answers'] = answers


async def test_importing_the_same_resume_again_writes_nothing(monkeypatch):
    fake = FakeStore()
    monkeypatch.setattr(store, 'upsert_candidate', fake.upsert_candidate)
    monkeypatch.setattr(store, 'apply', fake.apply)
    monkeypatch.setattr(load, 'known_resume', fake.known_resume)
    monkeypatch.setattr(load, 'save_prefill', fake.save_prefill)
    job = {'id': 7, 'questions': QUESTIONS}
    for item in all_formats():
        first = await load.import_item('selftest-intake', job, item)
        writes, applied = fake.writes, next(iter(fake.apps.values()))['applied']
        again = await load.import_item('selftest-intake', job, item)
        assert first['status'] in ('new', 'reapplied', 'same') and again['status'] == 'same'
        assert fake.writes == writes and next(iter(fake.apps.values()))['applied'] == applied
    # every format is one person and one application; each distinct text is one resume version
    assert len(fake.people) == 1 and len(fake.apps) == 1
    assert len(fake.resumes) == len({i['text'] for i in all_formats()})
    answers = next(iter(fake.apps.values()))['answers']
    assert answers['years_rn'] == {'value': '4 years', 'meets': 'yes', 'source': 'resume', 'confirmed': False}


async def test_a_resume_with_no_phone_email_or_license_is_skipped_not_guessed():
    out = await load.import_item('selftest-intake', {'id': 1}, extract.pasted('Pat Rivera\nBLS, 3 years ICU'))
    assert out['status'] == 'skipped'


# ---- job posts ----

def claude_reply(**extra) -> dict:
    """What a good model reply to tests/fixtures/job_post.txt looks like, plus whatever a test adds."""
    return {
        'facts': [{'key': 'title', 'value': 'Registered Nurse, Telemetry (Nights)',
                   'quote': 'Registered Nurse, Telemetry (Nights)'},
                  {'key': 'hospital', 'value': 'Hudson Park Bronx', 'quote': 'Hudson Park Bronx'},
                  {'key': 'shift', 'value': 'nights', 'quote': 'Three 12-hour night shifts a week'},
                  {'key': 'hours', 'value': '3 x 12 h', 'quote': 'Three 12-hour night shifts a week, 7 pm to 7 am'},
                  {'key': 'pay', 'value': '$52 to $64 an hour', 'quote': 'Pay: $52 to $64 an hour'},
                  *extra.get('facts', [])],
        'questions': [{'id': 'license', 'about': 'person', 'ask': 'Do you hold an active New York RN license?',
                       'good': 'yes', 'points': 20}, *extra.get('questions', [])],
        'criteria': [{'kind': 'required', 'name': 'NY RN license', 'points': 0, 'target_kind': 'license',
                      'target_value': 'NY RN', 'quote': 'Active New York State RN license (required)'},
                     {'kind': 'required', 'name': 'Current BLS and ACLS', 'points': 0, 'target_kind': 'cert',
                      'target_value': 'BLS, ACLS', 'quote': 'Current BLS and ACLS (required)'},
                     {'kind': 'required', 'name': 'Acute care', 'points': 0, 'target_kind': 'years',
                      'target_value': '1', 'quote': 'At least 1 year of acute care experience'},
                     {'kind': 'bonus', 'name': 'Med-surg', 'points': 5, 'target_kind': 'unit',
                      'target_value': 'med-surg', 'quote': 'Med-surg experience is a plus'},
                     *extra.get('criteria', [])]}


def test_a_drafted_job_keeps_what_the_post_says():
    clean, dropped = jobpost.ground(POST, claude_reply())
    assert not dropped and clean['title'] == 'Registered Nurse, Telemetry (Nights)'
    assert clean['shift'] == 'nights' and clean['pay'] == '$52 to $64 an hour'
    assert clean['hospital'] == 'Hudson Park Bronx'
    assert [c['target'] for c in clean['criteria']] == [{'license': 'NY RN'}, {'cert': 'BLS'}, {'cert': 'ACLS'},
                                                        {'years': 1.0}, {'unit': 'med-surg'}]


def test_invented_pay_bonus_and_criteria_are_left_out():
    clean, dropped = jobpost.ground(POST, claude_reply(
        facts=[{'key': 'sign_on_bonus', 'value': '$10,000', 'quote': 'a $10,000 sign-on bonus'},
               {'key': 'pay', 'value': '$70 an hour', 'quote': 'Pay: $52 to $64 an hour'}],
        criteria=[{'kind': 'required', 'name': 'CCRN', 'points': 0, 'target_kind': 'cert', 'target_value': 'CCRN',
                   'quote': 'CCRN required'}],
        questions=[{'id': 'kids', 'about': 'person', 'ask': 'Do you have children at home?', 'good': '',
                    'points': 0}]))
    assert 'sign_on_bonus' not in clean['facts'] and clean['pay'] == '$52 to $64 an hour'
    assert 'CCRN' not in [c['name'] for c in clean['criteria']] and 'kids' not in [q['id'] for q in clean['questions']]
    assert len(dropped) == 4   # the bonus, the $70 pay, the CCRN item and the question


words = st.text(st.sampled_from('abcdefgh $0123456789'), max_size=12)


@settings(max_examples=200)
@given(st.text(st.sampled_from('abcdefgh .,$0123456789\n'), min_size=1, max_size=200),
       st.lists(st.tuples(st.sampled_from(['pay', 'start', 'unit', 'perk']), words, words,
                          st.integers(0, 200), st.integers(0, 40)), max_size=6))
def test_every_kept_fact_is_quoted_from_the_post_and_its_numbers_are_in_the_quote(post, made):
    # Half the quotes are real slices of the post, half are made up; the values may hold any number
    facts = [{'key': k, 'value': v, 'quote': post[s:s + n] if s % 2 else q} for k, v, q, s, n in made]
    clean, _ = jobpost.ground(post, {'facts': facts, 'questions': [], 'criteria': []})
    kept = {**clean['facts'], **({'pay': clean['pay']} if clean['pay'] else {})}
    for key, value in kept.items():
        quote = clean['quotes'][key]
        assert re.sub(r'\s+', ' ', quote.lower()).strip() in re.sub(r'\s+', ' ', post.lower())
        assert jobpost.numbers(value) <= jobpost.numbers(quote)
    if not re.search(r'\d', post):
        assert clean['pay'] is None


class FakeOcto:
    def __init__(self):
        self.sql: list[tuple[str, list]] = []

    async def query(self, sql, params=None, row_limit=500):
        self.sql.append((sql, params or []))
        return [{'id': 1, 'status': 'draft' if sql.startswith('insert into jobs') else 'open'}]


async def test_a_drafted_job_is_a_draft_until_confirmed_in_its_own_sandbox(monkeypatch):
    fake = FakeOcto()
    monkeypatch.setattr(store.octo, 'query', fake.query)
    clean, dropped = jobpost.ground(POST, claude_reply())
    job = await jobpost.save('guest-0123456789ab', POST, clean, dropped)
    assert job['status'] == 'draft' and "'draft'" in fake.sql[1][0] and fake.sql[1][1][0] == 'guest-0123456789ab'
    await jobpost.confirm('guest-0123456789ab', 1)
    sql, params = next(s for s in fake.sql if s[0].startswith('update jobs'))
    assert "status = 'draft'" in sql and 'sandbox = $2' in sql and params == [1, 'guest-0123456789ab']


@given(st.one_of(st.none(), st.text(max_size=30), st.just('live'), st.just('selftest-intake'),
                 st.just('guest-0123456789ab')))
def test_the_web_page_only_ever_writes_to_a_visitor_sandbox(cookie):
    sandbox = load.visitor_sandbox(cookie)
    assert sandbox != 'live' and re.fullmatch(r'guest-[0-9a-f]{12}', sandbox)
    if cookie == 'guest-0123456789ab':
        assert sandbox == cookie


def test_intake_and_the_tracker_share_one_guest_cookie():
    # The tracker stays in pukujan-lab-2/nurse-screen (web/tracker/api.py COOKIE); its web server proxies /intake here
    from riley_web import server
    assert server.SANDBOX_COOKIE == "ns_guest"


def test_a_question_is_required_when_a_no_misses_a_required_item():
    """The job page's Screening tab shows Required from this (intake/load.py required): a knockout, a required
    criterion that names the question, or one whose target is what a yes tells us."""
    rn = {'kind': 'required', 'name': 'New York RN license', 'target': {'license': 'NY RN'}}
    bls = {'kind': 'required', 'name': 'current BLS', 'target': {'cert': 'BLS'}}
    nights = {'kind': 'weighted', 'name': 'nights', 'target': {'shift': 'nights'}}
    by_id = {'kind': 'required', 'name': 'Telemetry', 'target': {'question': 'tele'}}
    criteria = [rn, bls, nights, by_id]
    assert all(load.required(q, []) for q in QUESTIONS if q.get('knockout') or q.get('knockout_if'))
    assert load.required({'id': 'lic', 'ask': 'Your license?', 'fact': {'license': 'NY RN'}}, criteria)
    assert load.required({'id': 'c', 'ask': 'Certs?', 'fact': {'certs': ['ACLS', 'BLS']}}, criteria)
    assert load.required({'id': 'tele', 'ask': 'Telemetry?'}, criteria)
    # a posted job's question says what it asks about, with no fact
    assert load.required({'id': 'bls', 'ask': 'Is your BLS card current?'}, criteria)
    # a weighted item is not required; nor is a question about something else
    assert not load.required({'id': 'n', 'ask': 'Nights ok?', 'fact': {'shift': 'nights'}}, criteria)
    assert not load.required({'id': 'why', 'ask': 'Why this hospital?'}, criteria)
    assert not load.required({'id': 'blsx', 'ask': 'Do you like tables?', 'fact': {'cert': 'wound'}}, criteria)
