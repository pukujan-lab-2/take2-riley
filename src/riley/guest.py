"""Guests on the demo site: "call me" from /try, with a made-up name and a random open role (track 9).

A guest types a first name and a US mobile number. They get a `live` candidate under a made-up name, an
application to a random open job, and a call from Riley when an outbound trunk exists (riley/outbound.py); without
one, the page asks them to call Riley's number and the record is already waiting. What they typed (first name,
phone, email) sits in `candidates.guest` and the phone column, and both are wiped after two days by `purge`; the
made-up name, the application and the call records stay, so Dana can follow up in the demo.

The rules (is it a US mobile, may this number be called again, what the purge changes) are plain functions, so
tests check them without a database.
"""
import random
import re
import secrets
from datetime import UTC, datetime, timedelta

from . import match, octo, store

SANDBOX = 'live'
KEEP_DAYS = 2           # the form promises "my number is deleted after 2 days"
NUMBER_HOURS = 24       # one call per number per day
# Plain made-up full names, none a real public person; the guest plays this person on the call
NAMES = ['Jordan Lee', 'Sam Rivera', 'Alex Morgan', 'Casey Brooks', 'Taylor Reyes', 'Morgan Ellis', 'Jamie Park',
         'Riley Quinn', 'Avery Shah', 'Drew Castillo', 'Robin Okafor', 'Jesse Lin', 'Quinn Alvarez', 'Kai Bennett',
         'Rowan Diaz', 'Skyler Chen', 'Emerson Cole', 'Hayden Ruiz', 'Parker Nolan', 'Reese Patel']
# Riley is the agent's own name, so a guest is never called it
NAMES.remove('Riley Quinn')


def us_mobile(phone: str | None) -> str | None:
    """The ten digits of a dialable US number, or None. North American numbers: the area code and exchange never
    start with 0 or 1, and 555-01xx is reserved for fiction."""
    key = store.phone_key(phone)
    if not key or not re.fullmatch(r'[2-9]\d{2}[2-9]\d{6}', key):
        return None
    return None if store.fictional(key) else key


def e164(key: str) -> str:
    return f'+1{key}'


def refuse(now: datetime, consent: bool, phone: str | None, number_calls: list[datetime],
           ip_calls: list[datetime], today: int, cfg: dict) -> str | None:
    """Why this request may not go ahead, or None. `number_calls` are earlier guest sign-ups for this number,
    `ip_calls` this visitor's, `today` all guest sign-ups since midnight UTC. No call without the checkbox; a
    number at most once per 24 hours; a few per visitor per hour; a daily cap from screen.yaml."""
    g = cfg.get('guests', {})
    if not consent:
        return 'Please tick the box so Riley may call you.'
    if not us_mobile(phone):
        return 'That does not look like a US mobile number.'
    if any(now - t < timedelta(hours=NUMBER_HOURS) for t in number_calls):
        return 'This number already had a demo call today. Try again tomorrow.'
    if sum(now - t < timedelta(hours=1) for t in ip_calls) >= g.get('per_ip_hour', 3):
        return 'A few demos from here already this hour. Try again later.'
    if today >= g.get('daily_cap', 40):
        return 'Today’s demo calls are used up. Try again tomorrow.'
    return None


def scrub(row: dict, now: datetime) -> dict:
    """The candidate after the purge: once `expires` has passed, the phone and what the guest typed are gone and
    every other field is as it was. Before then, the row is unchanged."""
    expires = store.ts(row.get('expires'))
    if not row.get('guest') or not expires or now < expires:
        return row
    return {**row, 'guest': None, 'phone': None}


def needs_status_call(decided: dict | None) -> bool:
    """After `outcome.auto()`: an approved outcome rings by itself, and a second call would cross it. Anything else,
    a draft held for Dana by the bias check included, gets the short "you're in process" call."""
    return not (decided and decided.get('status') in ('approved', 'delivered'))


def made_up_name(rng: random.Random | None = None) -> str:
    return (rng or random).choice(NAMES)


# ---- the demo persona's resume: close to the job, not the same words (an ATS would miss it) ----

# The job's unit in plain words, as Riley says it
UNIT_WORDS = {'med-surg': 'med-surg', 'telemetry': 'telemetry', 'step-down': 'step-down', 'icu': 'ICU',
              'ed': 'emergency department', 'l&d': 'labor and delivery', 'peds': 'pediatrics', 'rehab': 'rehab',
              'long-term care': 'long-term care', 'nicu': 'NICU', 'psych': 'behavioral health',
              'dialysis': 'dialysis', 'or': 'operating room'}
# For each kind of unit a job needs, a neighbouring one for the resume: (how Riley says it, the resume's line, the
# match.UNITS key it reads as, or None). A keyword filter looking for the job's unit finds none of these.
NEAR = {
    'med-surg': ('the ICU', 'Medical ICU', 'icu'),
    'telemetry': ('step-down', 'Step-Down Unit', 'step-down'),
    'step-down': ('telemetry', 'Cardiac Telemetry', 'telemetry'),
    'icu': ('progressive care', 'Progressive Care Unit', 'step-down'),
    'ed': ('urgent care', 'Urgent Care Center', None),
    'l&d': ('postpartum', 'Mother-Baby Unit', None),
    'peds': ('the NICU', 'Neonatal ICU', 'nicu'),
    'nicu': ('pediatrics', 'Pediatric Unit', 'peds'),
    'psych': ('the emergency department', 'Emergency Department', 'ed'),
    'dialysis': ('the ICU', 'Surgical ICU', 'icu'),
    'or': ('PACU', 'Post-Anesthesia Care Unit', None),
    'rehab': ('an orthopedic floor', 'Orthopedic Unit', None),
    'long-term care': ('sub-acute rehab', 'Sub-Acute Rehab', 'rehab'),
}
# The shared screen.yaml questions a persona's resume answers outright, read back on the call for a yes
PLAIN = ('license', 'years_rn', 'certifications')
EMPLOYER = 'Northgate Community Hospital'  # made up, like every name in the demo


def _needs(job: dict, q: dict) -> str | None:
    """The kind of unit this job is about, as a match.UNITS key: from its title and unit, then its criteria, then
    the question itself. None when the job names no unit (a front desk, a lab draw team)."""
    for text in (job.get('title') or '', job.get('unit') or ''):
        if units := match.found(match.UNITS, text):
            return units[0]
    for c in job.get('criteria') or []:
        target = store._j_load(c.get('target')) or {}
        if isinstance(target.get('unit'), str) and target['unit'] in NEAR:
            return target['unit']
    fact = q.get('fact')
    if isinstance(fact, dict) and fact.get('unit') in NEAR:
        return fact['unit']
    units = match.found(match.UNITS, q.get('ask', ''))
    return units[0] if units else None


def gap(job: dict, questions: list[dict]) -> dict | None:
    """The one question the resume answers in other words: the job's own unit question first (an ICU job's "Have you
    worked in an ICU?"), else acute_care. {question, needs (the UNITS key), needs_words, near (UNITS key or None),
    says (how Riley says the resume's unit), line (the resume's unit name)}, or None when the job has no unit."""
    shared = {'acute_care'}
    unit_qs = [q for q in questions if q.get('fact') == 'units' or
               (isinstance(q.get('fact'), dict) and 'unit' in q['fact'])]
    q = next((q for q in unit_qs if q['id'] not in shared), None) or next(
        (q for q in unit_qs if q['id'] in shared), None)
    needs = _needs(job, q) if q else None
    if q is None or not needs or needs not in NEAR:
        return None
    says, line, near = NEAR[needs]
    return {'question': q['id'], 'needs': needs, 'needs_words': UNIT_WORDS[needs], 'near': near, 'says': says,
            'line': line}


def persona(job: dict, name: str, cfg: dict, rng: random.Random | None = None) -> dict:
    """The made-up resume a guest plays: an RN license, a few years, BLS and ACLS, all read back for a yes, and the
    job's unit question answered with a neighbouring unit (`gap`), so Riley asks one question about the actual work
    instead of filtering on the word. Returns {text, facts, answers (stored shape, not confirmed), gap, summary}."""
    pick = rng or random.Random()
    questions = store._j_load(job.get('questions')) or cfg.get('questions', [])
    ids = {q['id']: q for q in questions}
    years = pick.randint(2, 6)
    g = gap(job, questions)
    title = job.get('title') or cfg['agent'].get('job', 'RN')
    role = title.split(',')[0] if ',' in title else title
    unit = g['line'] if g else 'Medical-Surgical Unit'
    start = 2026 - years
    rn = 'license' in ids  # an RN job; other roles' resumes leave the RN license out
    text = '\n'.join([name, *(['Registered Nurse, New York State license'] if rn else []),
                      f'{role}, {unit}, {EMPLOYER}, {start} to present', 'BLS and ACLS, both current'])
    facts = {**({'licenses': ['NY RN']} if rn else {}), 'years': float(years), 'certs': ['ACLS', 'BLS']}
    if g and g['near']:
        facts['units'] = [g['near']]
    said = {'license': ('New York RN license', 'yes'),
            'years_rn': (f'{years} years as an RN', _years_meets(ids.get('years_rn'), years)),
            'certifications': ('BLS and ACLS, both current', 'yes')}
    answers = {i: {'value': v, 'meets': m, 'source': 'resume', 'confirmed': False}
               for i, (v, m) in said.items() if i in ids}
    if g:
        answers[g['question']] = {'value': f"{years} years in {g['says']} ({unit})", 'meets': 'unclear',
                                  'source': 'resume', 'confirmed': False, 'needs': g['needs_words']}
    summary = f"{years} years as an RN in {g['says']}" if g else f'{years} years as an RN'
    return {'text': text, 'facts': facts, 'answers': answers, 'gap': g, 'summary': summary}


def _years_meets(q: dict | None, years: int) -> str:
    m = re.fullmatch(r'at least (\d+)', str((q or {}).get('good', '')))
    return 'yes' if not m or years >= int(m.group(1)) else 'no'


def resume_answers(demo: dict) -> dict:
    """What the guest's resume says, in the screener's shape, waiting to be confirmed on the call."""
    return store.call_answers([{'id': demo['application_id'], 'answers': demo.get('answers')}],
                              demo['application_id'])[0]


def intro(who: dict, lang: str = 'en') -> str:
    """Said once on a guest call, after the language: who they play."""
    if lang == 'es':
        return (f"Para esta demostración, usted es {who['name']} y solicita el puesto de {who['title']} en "
                f"{who['hospital']}. ¿Es correcto?")
    return f"For this demo you're {who['name']}, applying for {who['title']} at {who['hospital']}. Is that right?"


# ---- reads and writes ----

async def _job(cfg: dict) -> dict:
    """A random open job from the seeded ones (riley/match.py), else the one screen.yaml describes."""
    try:
        jobs = await match.open_jobs(SANDBOX)
        if jobs:
            return random.choice(jobs)
    except Exception as e:  # no list: the screen.yaml job, so sign-up still works
        print('open jobs lookup failed:', type(e).__name__, e, flush=True)
    return await store.job_for(SANDBOX, cfg)


async def recent(key: str, since: datetime) -> list[datetime]:
    rows = await octo.query("select created from candidates where sandbox = $1 and guest->>'phone' = $2 "
                            'and created > $3', [SANDBOX, key, since])
    return [t for r in rows if (t := store.ts(r['created']))]


async def today(now: datetime) -> int:
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    rows = await octo.query('select count(*) as n from candidates where sandbox = $1 and guest is not null '
                            'and created >= $2', [SANDBOX, midnight])
    return int(rows[0]['n']) if rows else 0


async def create(first_name: str, phone: str, email: str | None, cfg: dict, now: datetime | None = None,
                 job: dict | None = None) -> dict:
    """The guest's candidate (made-up name, their phone so their own call finds it) and application, plus the
    token their status page uses, and a made-up resume close to the job but not in its words (`persona`). `job` is
    the open job they picked on /try; without one, a random open job.
    Returns {token, candidate_id, application_id, job_id, name, title, hospital, phone, resume}."""
    now = now or datetime.now(UTC)
    key = us_mobile(phone)
    assert key, 'checked by refuse() first'
    job = job or await _job(cfg)
    token = secrets.token_urlsafe(16)
    name = made_up_name()
    guest = {'first_name': first_name.strip()[:40], 'phone': key, 'email': (email or '').strip()[:120] or None,
             'token': token}
    who = (await octo.query(
        'insert into candidates (sandbox, name, phone, guest, expires, created) values ($1, $2, $3, $4, $5, $6) '
        'returning id', [SANDBOX, name, key, store._j(guest), now + timedelta(days=KEEP_DAYS), now]))[0]
    resume = persona(job, name, cfg)
    app = await store.apply(SANDBOX, who['id'], job['id'], resume=resume['text'], facts=resume['facts'],
                            source='guest', now=now)
    # The resume's answers wait on the application for Riley to confirm, and the one in other words to ask about
    await octo.query('update applications set answers = $2 where id = $1', [app['id'], store._j(resume['answers'])])
    await store.log(SANDBOX, 'guest', who['id'], app['id'], {'job_id': job['id']})
    return {'token': token, 'candidate_id': who['id'], 'application_id': app['id'], 'job_id': job['id'], 'name': name,
            'title': job['title'], 'hospital': job.get('hospital') or cfg['agent']['employer'], 'phone': key,
            'resume': resume['summary']}


async def find(phone: str) -> dict | None:
    """A guest record waiting for this number's call: the made-up name, the role and the application not yet
    screened. None for anyone else, so other callers get the usual name and role check."""
    rows = await octo.query(
        'select c.id as candidate_id, c.name, c.guest, a.id as application_id, a.answers, j.title, j.questions, '
        'j.facts, j.unit, j.shift, h.name as hospital from candidates c join applications a on a.candidate_id = c.id '
        'join jobs j on j.id = a.job_id left join hospitals h on h.id = j.hospital_id '
        "where c.sandbox = $1 and c.phone = $2 and c.guest is not null and a.status = 'applied' "
        'and a.withdrawn is null order by a.applied desc limit 1', [SANDBOX, store.phone_key(phone)])
    return rows[0] if rows else None


async def by_application(application_id: int) -> dict | None:
    rows = await octo.query(
        'select c.id as candidate_id, c.name, c.guest, a.id as application_id, a.answers, j.title, j.questions, '
        'j.facts, j.unit, j.shift, h.name as hospital from applications a join candidates c on c.id = a.candidate_id '
        'join jobs j on j.id = a.job_id left join hospitals h on h.id = j.hospital_id '
        'where a.id = $1', [application_id])
    return rows[0] if rows else None


async def by_token(token: str) -> dict | None:
    """What the guest's status page reads: their application, never anyone else's, found by the token only."""
    rows = await octo.query(
        'select c.id as candidate_id, c.name, c.language, a.id as application_id, a.status, a.decision, a.job_id, '
        'j.title, j.slots from candidates c join applications a on a.candidate_id = c.id '
        "join jobs j on j.id = a.job_id where c.sandbox = $1 and c.guest->>'token' = $2 "
        'order by a.applied desc limit 1', [SANDBOX, token])
    return rows[0] if rows else None


async def set_language(candidate_id: int, language: str) -> None:
    await octo.query('update candidates set language = $2 where id = $1', [candidate_id, language])


async def purge(now: datetime | None = None) -> int:
    """Wipe the phone and what guests typed once two days have passed (the rule is `scrub`). Returns how many."""
    rows = await octo.query('update candidates set guest = null, phone = null '
                            'where guest is not null and expires <= $1 returning id', [now or datetime.now(UTC)])
    return len(rows)

