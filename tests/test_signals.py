"""Seriousness signals, the license check and the tracker's three groups (track 10), without a model or a database.

The properties are in docs/BUILD-PLAN.md, "Properties and proof": signals come only from actions (rule 7), a no-show
never raises a group, a missing required item always shows in "missing" by name, nothing reads self_id or
accommodations, and a license is verified only on a real match.
"""
from datetime import UTC, datetime, timedelta

import httpx
from hypothesis import given
from hypothesis import strategies as st

from riley import signals as sig

NOW = datetime(2026, 11, 6, 15, tzinfo=UTC)
REQUIRED = ['license', 'work_authorization', 'certifications', 'nights']
CRITERIA = [{'id': i, 'kind': 'required', 'name': n} for i, n in enumerate(REQUIRED, 1)] + [
    {'id': 9, 'kind': 'weighted', 'name': 'years_rn', 'target': {'years': 1}}]

text = st.text(max_size=40)
meets = st.sampled_from(['yes', 'no', 'unclear'])
answer = st.fixed_dictionaries({'value': text, 'meets': meets, 'source': st.sampled_from(['call', 'resume']),
                                'confirmed': st.booleans()})
answers = st.dictionaries(st.sampled_from([*REQUIRED, 'years_rn', 'start_date', 'pay_expectation']), answer)
score = st.one_of(st.none(), st.fixed_dictionaries({
    'score': st.integers(0, 100), 'missing': st.lists(st.sampled_from(REQUIRED), max_size=2, unique=True)}))
application = st.fixed_dictionaries({
    'id': st.just(7), 'candidate_id': st.just(3), 'status': st.sampled_from(['applied', 'screened']),
    'applied': st.just(NOW - timedelta(days=2)), 'answers': answers, 'score': score})
event = st.one_of(
    st.fixed_dictionaries({'kind': st.just('callback'), 'detail': st.fixed_dictionaries({'answered': st.booleans()})}),
    st.fixed_dictionaries({'kind': st.sampled_from(['no_show', 'reconfirmed', 'booked', 'help', 'call', 'note']),
                           'detail': st.fixed_dictionaries({'words': text})}),
    st.fixed_dictionaries({'kind': st.just('license'), 'detail': st.fixed_dictionaries({
        'status': st.sampled_from(['verified', 'not_found', 'check']), 'url': st.just(sig.license_url('NY', '1'))})}))
events = st.lists(event, max_size=8)
SOUND = {'tone': 'nervous', 'accent': 'strong', 'emotion': 'upset', 'speech_rate': 3.2}
RANK = {'meets_all': 2, 'missing': 1, 'not_read': 0}


def reword(v):
    """Every piece of free text replaced, and how someone sounded added: none of it may change a signal."""
    if isinstance(v, dict):
        out = {k: 'x' * 7 if isinstance(x, str) and k in ('value', 'words', 'text', 'transcript') else reword(x)
               for k, x in v.items()}
        # Tagged on each record (an event, its detail, an answer, the application), as a careless writer might
        record = any(k in v for k in ('value', 'kind', 'status', 'words', 'answered'))
        return {**out, **(SOUND if record else {})}
    if isinstance(v, list):
        return [reword(x) for x in v]
    return v


def sort(app, evs):
    return sig.group(app, CRITERIA, sig.signals(evs, app), now=NOW)


# ---- signals ----

@given(events, application)
def test_signals_never_depend_on_wording_or_how_someone_sounds(evs, app):
    """Rule 7: only actions count. Rewording every answer and note, or tagging tone and accent, changes nothing."""
    assert sig.signals(reword(evs), reword(app)) == sig.signals(evs, app)
    assert sort(reword(app), reword(evs)) == sort(app, evs)


@given(events, application)
def test_one_more_chance_after_a_no_show_then_paused(evs, app):
    s = sig.signals(evs, app)
    no_shows = sum(e['kind'] == 'no_show' for e in evs)
    assert s['no_shows'] == no_shows
    assert s['paused'] == (no_shows >= 2)
    assert s['chances_left'] == max(0, 2 - no_shows)


def test_signals_from_actions():
    app = {'id': 7, 'status': 'screened', 'score': {'missing': []},
           'answers': {'license': {'value': 'yes', 'meets': 'yes', 'source': 'resume', 'confirmed': True},
                       'nights': {'value': 'sure', 'meets': 'yes', 'source': 'call'},
                       'start_date': {'value': 'in two weeks', 'meets': 'yes', 'source': 'call'}}}
    evs = [{'kind': 'callback', 'detail': {'answered': False}}, {'kind': 'callback', 'detail': {'answered': True}},
           {'kind': 'reconfirmed', 'detail': {}}]
    s = sig.signals(evs, app)
    assert s['finished'] and s['answered_directly'] and s['picked_up_callback'] and s['start_realistic']
    assert s['reconfirmed'] and s['no_shows'] == 0 and not s['paused'] and s['license'] is None

    vague = {**app, 'score': {'missing': ['nights']},
             'answers': {**app['answers'], 'nights': {'value': 'hmm', 'meets': 'unclear', 'source': 'call'}}}
    s = sig.signals([], vague)
    assert not s['finished'] and not s['answered_directly'] and s['picked_up_callback'] is None
    # Nothing known yet is unknown, not a mark against them
    blank = sig.signals([], {'id': 8, 'status': 'applied', 'answers': {}, 'score': None})
    assert blank['finished'] is None and blank['answered_directly'] is None and blank['start_realistic'] is None


# ---- groups ----

@given(events, application)
def test_a_no_show_never_raises_a_group(evs, app):
    before = sort(app, evs)
    after = sort(app, [*evs, {'kind': 'no_show', 'detail': {}}])
    assert RANK[after['group']] <= RANK[before['group']]


@given(events, application, st.sampled_from(REQUIRED))
def test_a_missing_required_item_lands_in_missing_by_name(evs, app, item):
    app = {**app, 'answers': {**app['answers'], item: {'value': 'no', 'meets': 'no', 'source': 'call',
                                                       'confirmed': True}}}
    g = sort(app, evs)
    assert g['group'] == 'missing' and item in g['missing'] and item in g['why']


@given(events, application)
def test_every_application_gets_a_group_and_a_reason(evs, app):
    g = sort(app, evs)
    assert g['group'] in sig.GROUPS and g['why'].strip()
    assert (g['group'] == 'missing') == bool(g['missing'])


@given(events, application, st.dictionaries(text, text), st.lists(text))
def test_group_never_reads_self_id_or_accommodations(evs, app, self_id, ways):
    """Rules 11 and 12: even handed them, the group and the signals ignore them."""
    told = {**app, 'self_id': self_id, 'accommodations': ways, 'race': 'any', 'sex': 'any'}
    assert sig.signals(evs, told) == sig.signals(evs, app)
    assert sort(told, evs) == sort(app, evs)


def test_groups_by_example():
    def ans(**m):
        return {k: {'value': '', 'meets': v, 'source': 'call', 'confirmed': True} for k, v in m.items()}
    all_yes = ans(license='yes', work_authorization='yes', certifications='yes', nights='yes')
    done = {'id': 1, 'status': 'screened', 'score': {'missing': []}, 'applied': NOW - timedelta(days=1)}
    assert sort({**done, 'answers': all_yes}, [])['group'] == 'meets_all'

    no_bls = sort({**done, 'answers': {**all_yes, **ans(certifications='no')}}, [])
    assert no_bls == {'group': 'missing', 'missing': ['certifications'], 'why': no_bls['why']}
    assert 'certifications' in no_bls['why']

    # Unanswered after the screen is missing too, said as not answered; before the screen it is "not read yet"
    half = sort({**done, 'answers': ans(license='yes', work_authorization='yes')}, [])
    assert half['group'] == 'missing' and half['missing'] == ['certifications', 'nights']
    assert 'not answered' in half['why']
    unread = sort({'id': 2, 'status': 'applied', 'score': None, 'answers': {}, 'applied': NOW - timedelta(days=3)}, [])
    assert unread['group'] == 'not_read' and '3 days' in unread['why']

    # An unconfirmed resume "no" is not held against them before they confirm it (rule 5)
    resume_no = {'certifications': {'value': 'BLS lapsed', 'meets': 'no', 'source': 'resume', 'confirmed': False}}
    assert sort({**done, 'status': 'applied', 'score': None, 'answers': resume_no}, [])['group'] == 'not_read'

    # Paused after two no-shows is said, and the group stays where the requirements put it
    paused = sort({**done, 'answers': all_yes}, [{'kind': 'no_show'}, {'kind': 'no_show'}])
    assert paused['group'] == 'meets_all' and 'paused' in paused['why']


def test_the_matcher_decides_requirements_when_it_ran():
    """With track 5's fit on the application, its missing and unknown items are the ones named."""
    app = {'id': 1, 'status': 'screened', 'score': {'missing': []}, 'answers': {},
           'fit': {'offer': False, 'met': ['NY RN license'], 'missing': [{'name': 'ACLS'}], 'unknown': []}}
    g = sig.group(app, CRITERIA, sig.signals([], app))
    assert g['group'] == 'missing' and g['missing'] == ['ACLS']


# ---- license ----

def test_license_link_dana_opens():
    url = sig.license_url('NY', '123456')
    assert url.startswith('https://eservices.nysed.gov/professions/verification-search?')
    assert 'licenseNumber=123456' in url and 'professionCode=022' in url
    assert sig.license_url('NY', '123456', 'LPN').endswith('professionCode=023')
    assert 'nursys' in sig.license_url('NJ', '123456')


@given(st.text(alphabet='0123456789', min_size=6, max_size=6), st.one_of(
    st.none(), st.text(), st.lists(st.dictionaries(text, text)),
    st.dictionaries(st.sampled_from(['licenseNumber', 'status', 'name', 'content']),
                    st.one_of(text, st.fixed_dictionaries({'label': text, 'value': text})))))
def test_license_verified_only_on_a_real_match(number, data):
    status = sig.read_lookup(number, data)
    assert status in ('verified', 'not_found', 'check')
    if status == 'verified':
        assert any(sig._field(r, 'licenseNumber').lstrip('0') == number.lstrip('0')
                   and sig._field(r, 'status').strip().upper() == 'REGISTERED' for r in sig._records(data))


def test_license_lookup_shapes():
    found = {'licenseNumber': {'label': 'License No', 'value': '123456'}, 'status': {'label': 'Status',
                                                                                  'value': 'Registered'}}
    assert sig.read_lookup('123456', found) == 'verified'
    assert sig.read_lookup('123456', {'licenseNumber': '123456', 'status': 'REGISTERED'}) == 'verified'
    assert sig.read_lookup('123456', {'content': [{'licenseNumber': '123456', 'status': 'Registered'}]}) == 'verified'
    assert sig.read_lookup('123456', {**found, 'status': {'value': 'Not Registered'}}) == 'check'
    assert sig.read_lookup('654321', found) == 'check'      # someone else's record is never a match
    assert sig.read_lookup('123456', {}) == 'not_found'
    assert sig.read_lookup('123456', {'content': []}) == 'not_found'
    assert sig.read_lookup('123456', '<html>Access Denied</html>') == 'check'


async def test_blocked_or_failed_lookup_gives_check(monkeypatch):
    monkeypatch.delenv('NYSED_ROSA_KEY', raising=False)
    r = await sig.license('NY', '123456')
    assert r == {'status': 'check', 'url': sig.license_url('NY', '123456')}

    monkeypatch.setenv('NYSED_ROSA_KEY', 'test')

    def answer(status, body):
        async def get(self, url, **kw):
            return httpx.Response(status, text=body, request=httpx.Request('GET', url))
        monkeypatch.setattr(httpx.AsyncClient, 'get', get)

    answer(403, '<html>Access Denied | NYSED</html>')
    assert (await sig.license('NY', '123456'))['status'] == 'check'
    answer(200, '<html>Access Denied | NYSED</html>')
    assert (await sig.license('NY', '123456'))['status'] == 'check'
    answer(200, '{"licenseNumber": {"value": "123456"}, "status": {"value": "Registered"}}')
    assert (await sig.license('NY', '123456'))['status'] == 'verified'

    async def down(self, url, **kw):
        raise httpx.ConnectTimeout('timed out')
    monkeypatch.setattr(httpx.AsyncClient, 'get', down)
    assert (await sig.license('NY', '123456'))['status'] == 'check'
    assert (await sig.license('NY', ''))['status'] == 'check'


# ---- for the tracker ----

async def test_for_tracker_reads_no_protected_tables(monkeypatch):
    """The tracker's rows: a group, a reason and signals each, from SQL that never touches self_id or
    accommodations, with the license looked up once and cached as an event."""
    sql, logged = [], []

    async def query(q, params=None, row_limit=500):
        sql.append(q)
        if 'from events' in q:
            return [{'application_id': 7, 'candidate_id': 3, 'kind': 'no_show', 'detail': {}}]
        if 'from applications' in q:
            return [{'id': 7, 'candidate_id': 3, 'status': 'screened', 'applied': NOW.isoformat(), 'withdrawn': None,
                     'answers': {'certifications': {'value': 'no', 'meets': 'no', 'source': 'call'}},
                     'score': {'missing': []}, 'license': 'NY-123456', 'facts': {}}]
        return CRITERIA if 'from criteria' in q else []

    async def ensure():
        pass

    async def log(sandbox, kind, candidate_id=None, application_id=None, detail=None, due=None):
        logged.append((sandbox, kind, candidate_id, detail))

    monkeypatch.delenv('NYSED_ROSA_KEY', raising=False)
    monkeypatch.setattr(sig.octo, 'query', query)
    monkeypatch.setattr(sig.octo, 'ready', lambda: True)
    monkeypatch.setattr(sig.store, 'ensure', ensure)
    monkeypatch.setattr(sig.store, 'log', log)
    rows = await sig.for_tracker('selftest-signals', 1)
    assert len(rows) == 1 and rows[0]['group'] == 'missing' and rows[0]['missing'][0] == 'certifications'
    assert rows[0]['signals']['no_shows'] == 1 and rows[0]['signals']['license'] == 'check'
    assert rows[0]['license_url'] == sig.license_url('NY', '123456')
    assert logged and logged[0][:3] == ('selftest-signals', 'license', 3)
    assert sql and not any('self_id' in q or 'accommodations' in q for q in sql)
