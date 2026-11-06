"""Seriousness signals, the license check and the tracker's three groups (SPEC §4 and §5, BUILD-PLAN track 10).

Signals come only from what someone did: finished the screen, answered the missing items directly, picked up the
callback they chose, gave a realistic start date, reconfirmed, and no-shows. They read event kinds, flags and the
rubric's verdicts, never free text, and never tone, accent, emotion or speed of speech (rule 7).

Sorting puts every application in one of three groups, each with a reason: meets every required item, missing
something (named), or not read yet (how long). No group rejects anyone; Dana opens every one. Nothing here reads
self-reported demographics or accommodations (rules 11 and 12).

Events this reads, written by the screen, the scheduler and the outcome calls:
  callback {answered: bool}   Riley rang at the time they chose
  no_show                     they missed a booked interview
  reconfirmed                 they confirmed a booked interview
  license {state, number, status, url}   a lookup below, kept so it runs at most once a day per number
"""
import asyncio
import importlib
import os
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx

from . import octo, store

GROUPS = ('meets_all', 'missing', 'not_read')
NO_SHOW_CHANCES = 1   # a no-show is told plainly and gets one more chance, then the application is paused

NYSED_SEARCH = 'https://eservices.nysed.gov/professions/verification-search'
# The JSON service behind that page. It refuses requests without a key (403 Access Denied), so the lookup runs only
# when NYSED_ROSA_KEY is set; otherwise Dana gets the link and opens it herself.
NYSED_LOOKUP = 'https://api.nysed.gov/rosa/V2/byProfessionAndLicenseNumber'
PROFESSIONS = {'RN': '022', 'LPN': '023'}   # NYSED profession codes
OTHER_STATES = 'https://www.nursys.com/'      # every other state's boards answer through Nursys
LICENSE_CACHE = timedelta(days=1)             # NYSED updates its records once a working day


# ---- signals, without a database ----

def _detail(e: dict) -> dict:
    return store._j_load(e.get('detail')) or {}


def _confirmed(v: dict) -> bool:
    # Only confirmed answers count (rule 5); a call answer is confirmed as it is said
    return bool(v.get('confirmed', v.get('source') != 'resume'))


def signals(events: list[dict], application: dict) -> dict:
    """What this person did, from events (oldest first) and the application's recorded answers.

    None means not known yet, which is never held against anyone.
    """
    answers = store._j_load(application.get('answers')) or {}
    score = store._j_load(application.get('score'))
    asked = [v for v in answers.values() if v.get('source') == 'call']
    callbacks = [bool(_detail(e).get('answered')) for e in events if e.get('kind') == 'callback']
    licenses = [_detail(e).get('status') for e in events if e.get('kind') == 'license']
    no_shows = sum(e.get('kind') == 'no_show' for e in events)
    start = answers.get('start_date')
    return {
        'finished': None if not score else not score.get('missing'),
        # The items the resume didn't cover, answered on the call with a clear yes or no from the rubric
        'answered_directly': all(v.get('meets') in ('yes', 'no') for v in asked) if asked else None,
        'picked_up_callback': callbacks[-1] if callbacks else None,
        # The job's rubric for start_date (screen.yaml: within 6 weeks) also covers the notice they owe
        'start_realistic': start.get('meets') == 'yes' if start and _confirmed(start) else None,
        'reconfirmed': any(e.get('kind') == 'reconfirmed' for e in events),
        'no_shows': no_shows,
        'chances_left': max(0, NO_SHOW_CHANCES + 1 - no_shows),
        'paused': no_shows > NO_SHOW_CHANCES,
        'license': licenses[-1] if licenses else None,
    }


def _name(item) -> str:
    return str(item.get('name') if isinstance(item, dict) else item)


def requirements(application: dict, criteria: list[dict]) -> tuple[list[str], list[str], list[str]]:
    """The job's required items as (met, answered no, not known). The matcher's fit decides when it ran (track 5);
    otherwise each item is checked against the confirmed answer of the same id (or its target's `question`)."""
    fit = application.get('fit')
    if fit:
        return ([_name(i) for i in fit.get('met', [])], [_name(i) for i in fit.get('missing', [])],
                [_name(i) for i in fit.get('unknown', [])])
    answers = store._j_load(application.get('answers')) or {}
    met, no, unknown = [], [], []
    for c in criteria:
        if c.get('kind') != 'required':
            continue
        a = answers.get((store._j_load(c.get('target')) or {}).get('question') or c['name'])
        verdict = a.get('meets') if a and _confirmed(a) else None
        (met if verdict == 'yes' else no if verdict == 'no' else unknown).append(c['name'])
    return met, no, unknown


def group(application: dict, criteria: list[dict], signals: dict, now: datetime | None = None) -> dict:
    """Which of the three groups this application sits in, why, and the required items it is missing."""
    met, no, unknown = requirements(application, criteria)
    screened = bool(store._j_load(application.get('score'))) or application.get('status') not in (None, 'applied')
    if no:
        missing = no + (unknown if screened else [])
        why = 'Missing: ' + ', '.join(no) + (f"; not answered: {', '.join(unknown)}" if screened and unknown else '')
        out = {'group': 'missing', 'missing': missing, 'why': why}
    elif not screened:
        applied = store.ts(application.get('applied'))
        age = f', applied {(now - applied).days} days ago' if now and applied else ''
        out = {'group': 'not_read', 'missing': [], 'why': f'Not read yet: the screen is not done{age}'}
    elif unknown:
        out = {'group': 'missing', 'missing': unknown, 'why': 'Required items not answered: ' + ', '.join(unknown)}
    else:
        out = {'group': 'meets_all', 'missing': [],
               'why': 'Has every required item' + (f": {', '.join(met)}" if met else ' (this job lists none)')}
    # Said alongside, never a reason to move anyone down or out
    if signals.get('paused'):
        out['why'] += f"; paused after {signals['no_shows']} missed interviews, Riley offers a new time"
    if signals.get('license') == 'not_found':
        out['why'] += '; the license number was not found, Riley asks them to confirm it'
    return out


# ---- the license check ----

def license_url(state: str, number: str, profession: str = 'RN') -> str:
    """The public verification page Dana opens for this license."""
    if (state or 'NY').upper() != 'NY':
        return OTHER_STATES
    code = PROFESSIONS.get(profession, profession)
    return f'{NYSED_SEARCH}?{urlencode({"licenseNumber": number, "professionCode": code})}'


def _field(record: dict, key: str) -> str:
    # The service returns each field either plain or as {label, value}
    v = record.get(key)
    return str((v.get('value') if isinstance(v, dict) else v) or '')


def _records(data) -> list[dict]:
    if isinstance(data, dict):
        content = data.get('content')
        return [r for r in content if isinstance(r, dict)] if isinstance(content, list) else [data]
    return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []


def read_lookup(number: str, data) -> str:
    """'verified' only when a record has this exact number and its status is Registered. No record at all is
    'not_found'; anything else (another number, another status, a page instead of data) is 'check'."""
    if not isinstance(data, (dict, list)):
        return 'check'
    records = [r for r in _records(data) if _field(r, 'licenseNumber')]
    if not records:
        return 'not_found'
    want = number.lstrip('0')
    for r in records:
        if want and _field(r, 'licenseNumber').lstrip('0') == want and _field(r, 'status').strip().upper() == \
                'REGISTERED':
            return 'verified'
    return 'check'


async def _lookup(state: str, number: str, profession: str) -> str:
    key = os.environ.get('NYSED_ROSA_KEY')
    if state != 'NY' or not key:
        return 'check'
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            r = await http.get(NYSED_LOOKUP, headers={'x-oapi-key': key, 'User-Agent': 'nurse-screen'},
                               params={'professionCode': PROFESSIONS.get(profession, profession),
                                       'licenseNumber': number})
        return read_lookup(number, r.json()) if r.status_code == 200 else 'check'
    except (httpx.HTTPError, ValueError):
        return 'check'


async def license(state: str, number: str, *, sandbox: str | None = None, candidate_id: int | None = None,
                  profession: str = 'RN') -> dict:
    """Look the license up on the NYSED Office of the Professions. Returns {'status': 'verified' | 'not_found' |
    'check', 'url'}; 'check' means Dana opens the link. With a sandbox, the result is kept as a `license` event and
    a verified or not-found answer from the last day is reused."""
    state = (state or 'NY').upper()
    number = re.sub(r'\D', '', number or '')
    url = license_url(state, number, profession)
    if not number:
        return {'status': 'check', 'url': url}
    keep = sandbox is not None and octo.ready()
    if keep:
        rows = await octo.query(
            "select detail from events where sandbox = $1 and kind = 'license' and detail->>'state' = $2 "
            "and detail->>'number' = $3 and at > $4 order by at desc limit 1",
            [sandbox, state, number, datetime.now(UTC) - LICENSE_CACHE])
        last = _detail(rows[0]) if rows else {}
        if last.get('status') in ('verified', 'not_found'):
            return {'status': last['status'], 'url': url}
    result = {'status': await _lookup(state, number, profession), 'url': url}
    if keep and sandbox:
        await store.log(sandbox, 'license', candidate_id, None, {'state': state, 'number': number, **result})
    return result


# ---- for the tracker ----

def _split_license(key: str) -> tuple[str, str]:
    """'NY-123456' -> ('NY', '123456'); a bare number is taken as New York's."""
    m = re.match(r'([A-Z]{2})-?(\d+)$', key or '')
    return (m.group(1), m.group(2)) if m else ('NY', re.sub(r'\D', '', key or ''))


async def for_tracker(sandbox: str, job_id: int, now: datetime | None = None) -> list[dict]:
    """Every application to this job with its group, reason and signals, for Dana's tracker."""
    now = now or datetime.now(UTC)
    await store.ensure()
    apps = await octo.query(
        'select a.id, a.candidate_id, a.status, a.applied, a.withdrawn, a.answers, a.score, c.license, c.facts '
        'from applications a join candidates c on c.id = a.candidate_id '
        'where a.sandbox = $1 and a.job_id = $2 order by a.applied', [sandbox, job_id])
    if not apps:
        return []
    criteria = await octo.query('select * from criteria where job_id = $1 order by id', [job_id])
    events = await octo.query(
        'select application_id, candidate_id, kind, detail, at from events where sandbox = $1 and ('
        'application_id in (select id from applications where sandbox = $1 and job_id = $2) or '
        '(application_id is null and candidate_id in (select candidate_id from applications where sandbox = $1 '
        'and job_id = $2))) order by at', [sandbox, job_id])
    try:  # the matcher (track 5) decides requirements once it is on main
        match = importlib.import_module('agent.match')
        job = (await octo.query('select * from jobs where id = $1', [job_id]))[0]
    except (ImportError, IndexError):
        match = job = None

    async def checked(a: dict) -> dict:
        """The license result, looked up (and cached) when none is on file yet."""
        state, number = _split_license(a.get('license') or '')
        url = license_url(state, number)
        if not number:
            return {}
        if any(e['kind'] == 'license' for e in mine(a)):
            return {'url': url}
        return await license(state, number, sandbox=sandbox, candidate_id=a['candidate_id'])

    def mine(a: dict) -> list[dict]:
        return [e for e in events if e.get('application_id') == a['id']
                or (e.get('application_id') is None and e.get('candidate_id') == a['candidate_id'])]

    looked = await asyncio.gather(*(checked(a) for a in apps))
    rows = []
    for a, lic in zip(apps, looked, strict=True):
        evs = mine(a) + ([{'kind': 'license', 'detail': lic}] if lic.get('status') else [])
        if match and job:
            a = {**a, 'fit': match.fit(store._j_load(a.get('facts')) or {}, job, criteria)}
        s = signals(evs, a)
        rows.append({'application_id': a['id'], 'candidate_id': a['candidate_id'], 'status': a['status'],
                     **group(a, criteria, s, now), 'signals': s, 'license_url': lic.get('url')})
    return rows
