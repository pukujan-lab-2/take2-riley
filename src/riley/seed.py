"""The demo hospitals and jobs from jobs/seed.yaml, written into one sandbox (docs/BUILD-PLAN.md, "Jobs, hospitals and
shifts").

Safe to run again: a hospital is found by name and a job by title and hospital, then brought up to date; a criterion
is found by its job and name, so its id (which a decision may name) stays the same. The job screen.yaml describes is
the seeded Midtown med-surg job: the row it made before the seed is taken over, never doubled.

A visitor's sandbox (never `live`) also gets a small cast of made-up candidates with screened applications
(docs/BUILD-PLAN.md, "Guest access for now"), so Dana's tracker shows every state the demo uses. Their numbers are in
the 555-01xx fiction range and they are marked `facts.demo = 'tracker'`, so nothing ever rings them.

Run:  .venv/bin/python -m agent.seed --sandbox live
"""
import argparse
import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

from . import keys, match, octo, outcome, store

ROOT = Path(__file__).resolve().parents[2]
SEED = ROOT / 'jobs' / 'seed.yaml'


def screen_config() -> dict:
    return yaml.safe_load((ROOT / 'screen.yaml').read_text())


def jobs(cfg: dict | None = None) -> tuple[list[dict], list[dict]]:
    """The hospitals and the jobs of seed.yaml, each job with its questions written out in full (screen.yaml's
    shape) and screen.yaml's other_roles question asked last."""
    cfg = cfg or screen_config()
    data = yaml.safe_load(SEED.read_text())
    shared = {q['id']: q for q in cfg['questions']}
    out = []
    for j in data['jobs']:
        if j.get('from_screen'):
            j = {**j, 'facts': cfg['job_facts'], 'questions': cfg['questions']}
        qs = [shared[q['use']] if 'use' in q else q for q in j['questions']]
        if 'other_roles' not in {q['id'] for q in qs}:
            qs.append(shared['other_roles'])
        # YAML reads the times as datetimes; jobs.slots keeps them as ISO text
        slots = [t.isoformat() if hasattr(t, 'isoformat') else t for t in j.get('slots', data['slots'])]
        out.append({'options': [], **j, 'slots': slots, 'questions': qs})
    return data['hospitals'], out


def demo_jobs(cfg: dict | None = None) -> list[dict]:
    """The seeded jobs as `match.open_jobs` returns them, numbered from 1, without a database: for tests."""
    hospitals, js = jobs(cfg)
    borough = {h['name']: h['borough'] for h in hospitals}
    return [{**j, 'id': i, 'borough': borough[j['hospital']], 'status': 'open', 'window_hours': 0,
             'criteria': [{'id': i * 100 + k, 'job_id': i, **c} for k, c in enumerate(j['criteria'])]}
            for i, j in enumerate(js, 1)]


async def seed(sandbox: str) -> None:
    await store.ensure()
    cfg = screen_config()
    hospitals, js = jobs(cfg)
    ids = {}
    for h in hospitals:
        rows = await octo.query('select id from hospitals where sandbox = $1 and name = $2', [sandbox, h['name']])
        if not rows:
            rows = await octo.query('insert into hospitals (sandbox, name, borough, address) values ($1, $2, $3, $4) '
                                    'returning id', [sandbox, h['name'], h['borough'], h['address']])
        ids[h['name']] = rows[0]['id']
    for j in js:
        hid = ids[j['hospital']]
        rows = await octo.query('select id from jobs where sandbox = $1 and title = $2 and hospital_id = $3',
                                [sandbox, j['title'], hid])
        if not rows and j.get('from_screen'):  # the row screen.yaml made before there was a seed
            rows = await octo.query('select id from jobs where sandbox = $1 and title = $2 and hospital_id is null',
                                    [sandbox, cfg['agent']['title']])
        cols = {'title': j['title'], 'hospital_id': hid, 'location': j['hospital'], 'unit': j['unit'],
                'shift': j['shift'], 'hours': j['hours'], 'pay': j['pay'], 'facts': store._j(j['facts']),
                'questions': store._j(j['questions']), 'slots': store._j(j['slots']),
                'options': store._j(j['options']), 'window_hours': 0, 'status': 'open'}
        if rows:
            sets = ', '.join(f'{k} = ${i}' for i, k in enumerate(cols, 2))
            await octo.query(f'update jobs set {sets} where id = $1', [rows[0]['id'], *cols.values()])
            job_id = rows[0]['id']
        else:
            marks = ', '.join(f'${i}' for i in range(2, len(cols) + 2))
            job_id = (await octo.query(f'insert into jobs (sandbox, {", ".join(cols)}) values ($1, {marks}) '
                                       'returning id', [sandbox, *cols.values()]))[0]['id']
        for c in j['criteria']:
            vals = [c['kind'], c.get('points', 0), store._j(c['target']), c.get('reason'), c.get('change')]
            rows = await octo.query('update criteria set kind = $3, points = $4, target = $5, reason = $6, '
                                    'change = $7 where job_id = $1 and name = $2 returning id',
                                    [job_id, c['name'], *vals])
            if not rows:
                await octo.query('insert into criteria (job_id, name, kind, points, target, reason, change) '
                                 'values ($1, $2, $3, $4, $5, $6, $7)', [job_id, c['name'], *vals])
    cast = await seed_cast(sandbox, js)
    print(f'seeded {sandbox}: {len(hospitals)} hospitals, {len(js)} jobs, {cast} made-up candidates', flush=True)


# ---- the guest cast: made-up candidates for a visitor's sandbox ----

DEMO = 'tracker'           # candidates.facts.demo for the cast; the bias seed's people are 'bias'
DECIDED_BY = 'Dana (made-up demo)'

# One entry per person. answers: {question id: (what they said, meets)}; `resume` answers are not confirmed yet.
# decided: Dana's result and the criteria she ticked; message: the draft's status. All names and numbers made up.
CAST = [
    {'name': 'Maya Brennan', 'phone': '2125550141', 'job': 'RN, Medical-Surgical', 'hours_ago': 30,
     'answers': {'license': ('Yes, active New York RN license', 'yes'), 'work_authorization': ('Yes', 'yes'),
                 'years_rn': ('4 years', 'yes'), 'acute_care': ('Med-surg for 3 years, then telemetry', 'yes'),
                 'certifications': ('BLS and ACLS, both current', 'yes'), 'nights': ('Yes, nights suit me', 'yes'),
                 'start_date': ('In three weeks, after my notice', 'yes'),
                 'pay_expectation': ('Around the middle of the range', 'yes'), 'other_roles': ('Yes', 'yes')},
     'transcript': [('assistant', 'Hi, this is Riley from Hudson Park. Do you have a few minutes for the screen?'),
                    ('user', 'Sure, I just got off shift.'),
                    ('assistant', 'Do you hold an active Registered Nurse license in New York State?'),
                    ('user', 'Yes, it is active.'),
                    ('assistant', 'How many years have you worked as a Registered Nurse?'),
                    ('user', 'Four years. Three on med-surg, then telemetry.'),
                    ('assistant', 'Thanks. Dana reads every screen, and you will hear from us within two days.')]},
    {'name': 'Nina Castillo', 'phone': '2125550142', 'job': 'RN, ICU', 'hours_ago': 22,
     'answers': {'license': ('Yes, New York RN', 'yes'), 'work_authorization': ('Yes', 'yes'),
                 'years_rn': ('6 years', 'yes'), 'acute_care': ('Step-down and MICU', 'yes'),
                 # the item she is missing; worded so matching reads no advanced cardiac card either
                 'certifications': ('BLS is current; the advanced one ran out in August', 'no'),
                 'icu': ('Two years in a MICU', 'yes'), 'days': ('Yes, days work', 'yes'),
                 'ccrn': ('Not yet', 'no'), 'start_date': ('In a month', 'yes'),
                 'pay_expectation': ('Within the range', 'yes'), 'other_roles': ('Yes', 'yes')},
     'transcript': [('assistant', 'Is your BLS certification current? And do you also have ACLS?'),
                    ('user', 'BLS yes. The advanced one ran out in August, I keep meaning to book the class.'),
                    ('assistant', 'Thanks, I have noted that. Have you worked in an ICU or other critical care unit?'),
                    ('user', 'Two years in a medical ICU.'),
                    ('assistant', 'Good. Dana reads every screen, and you will hear within two days.')]},
    {'name': 'Theo Marsh', 'phone': '2125550143', 'job': 'RN, Medical-Surgical', 'hours_ago': 40,
     'answers': {'license': ('Yes', 'yes'), 'work_authorization': ('Yes', 'yes'), 'years_rn': ('2 years', 'yes'),
                 'acute_care': ('Med-surg at a community hospital', 'yes'),
                 'certifications': ('Lapsed last month; the renewal class is booked', 'no'),
                 'nights': ('Yes', 'yes'), 'start_date': ('Two weeks', 'yes'),
                 'pay_expectation': ('The low end is fine', 'yes'), 'other_roles': ('No', 'no')},
     'decided': {'result': 'not_yet', 'criteria': ['current BLS']}, 'message': 'draft',
     'transcript': [('assistant', 'Is your BLS certification current? And do you also have ACLS?'),
                    ('user', 'It lapsed last month. I have the renewal class booked.'),
                    ('assistant', 'Thanks for telling me. The role is three 12-hour night shifts a week. Does that '
                                  'work for you?'),
                    ('user', 'Yes, nights are fine.'),
                    ('assistant', 'Thank you. Dana reads every screen, and you will hear within two days.')]},
    {'name': 'Grace Okafor', 'phone': '2125550144', 'job': 'RN, Medical-Surgical', 'hours_ago': 46,
     'answers': {'license': ('Yes', 'yes'), 'work_authorization': ('Yes', 'yes'), 'years_rn': ('7 years', 'yes'),
                 'acute_care': ('Med-surg and telemetry', 'yes'), 'certifications': ('BLS and ACLS', 'yes'),
                 'nights': ('Yes, I work nights now', 'yes'), 'start_date': ('A month from now', 'yes'),
                 'pay_expectation': ('The top of the range', 'yes'), 'other_roles': ('Yes', 'yes')},
     'decided': {'result': 'next_round', 'criteria': []}, 'message': 'delivered', 'booked_with': 'nurse',
     'transcript': [('assistant', 'Hi Grace, this is Riley from Hudson Park, calling about the med-surg role. Is '
                                  'now a good time?'),
                    ('user', 'Yes, go ahead.'),
                    ('assistant', 'Good news: Dana would like to move you forward to a second interview. Which '
                                  'time works, and would you like to meet Dana or a nurse from the unit?'),
                    ('user', 'The first time, with a nurse from the unit.'),
                    ('assistant', 'Booked. You will get a reminder the day before.')]},
    {'name': 'Lucía Serrano', 'phone': '2125550145', 'job': 'RN, Telemetry', 'hours_ago': 18, 'language': 'es',
     # Riley records answers in a few job-related words; the call itself was in Spanish
     'answers': {'license': ('Yes, New York RN', 'yes'), 'work_authorization': ('Yes', 'yes'),
                 'years_rn': ('5 years', 'yes'), 'acute_care': ('Telemetry and step-down', 'yes'),
                 'certifications': ('BLS and ACLS, current', 'yes'), 'nights': ('Yes', 'yes'),
                 'start_date': ('In two weeks', 'yes'), 'pay_expectation': ('Within the range', 'yes'),
                 'other_roles': ('Yes', 'yes')},
     'transcript': [('assistant', 'Hola, soy Riley de Hudson Park. ¿Prefiere seguir en español?'),
                    ('user', 'Sí, en español, por favor.'),
                    ('assistant', '¿Tiene una licencia de enfermera registrada activa en el estado de Nueva York?'),
                    ('user', 'Sí, activa.'),
                    ('assistant', '¿Cuántos años ha trabajado como enfermera registrada?'),
                    ('user', 'Cinco años, en telemetría y step-down.'),
                    ('assistant', 'Gracias. Dana lee cada entrevista y le respondemos en dos días.')]},
    {'name': 'Sam Whitfield', 'phone': '2125550146', 'job': 'LPN, Rehab', 'hours_ago': 6, 'status': 'applied',
     'resume': {'license_lpn': ('New York LPN', 'yes')},
     'transcript': [('assistant', 'Hi, this is Riley from Hudson Park. Do you have a few minutes for the screen?'),
                    ('user', "I'm on my way into a shift. Can you call me tomorrow morning?"),
                    ('assistant', 'Of course. You can also call this number any time to finish it.')]},
    {'name': 'Priya Raman', 'phone': '2125550147', 'job': 'RN, Telemetry', 'hours_ago': 3, 'status': 'applied',
     'resume': {'years_rn': ('5 years', 'yes'), 'acute_care': ('Step-down, 2 years', 'yes'),
                'certifications': ('BLS, ACLS', 'yes')},
     'events': ['help'],   # she rang the help line to check: the tracker tags her "called to check"
     'transcript': [('user', 'Hi, I sent my resume this morning for the telemetry role. Did it come through?'),
                    ('assistant', 'It did. Your screen is not done yet; you can do it now or any time on this '
                                  'number.'),
                    ('user', 'I will call back tonight.'),
                    ('assistant', 'Sounds good. You will hear from us within two days either way.')]},
]


def cast(js: list[dict], now: datetime | None = None, cfg: dict | None = None) -> list[dict]:
    """The guest cast as rows to write, built with the real shapes: answers as `store.stored_answers` keeps them,
    the score from the screener's own scorecard, facts as matching reads them, and each decided person's message
    from `outcome.parts_for` and `outcome.template`, so every fact in it is in its parts. `js` are jobs with ids
    and criteria ids (`demo_jobs`, or the sandbox's own rows)."""
    from .screener import Screen  # loads livekit.agents, so only when a cast is built
    now = now or datetime.now(UTC)
    cfg = cfg or screen_config()
    by_title = {j['title']: j for j in js}
    people = []
    for p in CAST:
        job = by_title.get(p['job'])
        if not job:
            continue
        said = {q: {'answer': a, 'meets': m, 'source': 'call', 'confirmed': True}
                for q, (a, m) in p.get('answers', {}).items()}
        said |= {q: {'answer': a, 'meets': m, 'source': 'resume', 'confirmed': False}
                 for q, (a, m) in p.get('resume', {}).items()}
        screened = p.get('status') != 'applied'
        score = Screen(cfg={'questions': job['questions']}, answers=said).scorecard() if screened else None
        facts = match.facts_from(job['questions'], said)
        consent = {'other_roles': facts.pop('other_roles')} if 'other_roles' in facts else {}
        applied = now - timedelta(hours=p['hours_ago'])
        row = {'key': f"{p['name'].split()[0].lower()}.demo-cast@example.invalid", 'name': p['name'],
               'phone': p['phone'], 'language': p.get('language', 'en'), 'consent': consent,
               'facts': {**facts, 'demo': DEMO}, 'job_id': int(job['id']), 'applied': applied.isoformat(),
               'promised_by': store.promised_by(applied).isoformat(),
               'status': 'screened' if screened else 'applied', 'answers': store.stored_answers(said),
               'events': [{'kind': 'transcript', 'detail': {'turns': [{'role': r, 'text': t}
                                                                      for r, t in p['transcript']]}}]
               + [{'kind': k, 'detail': {}} for k in p.get('events', [])]}
        if score:
            row['score'] = score
        if p.get('decided'):
            row.update(_decided(p, job, row, now, cfg))
        people.append(row)
    return people


def _decided(p: dict, job: dict, row: dict, now: datetime, cfg: dict) -> dict:
    """Dana's decision, Riley's message from its parts, and for a next round given on a call, the time they booked."""
    result = p['decided']['result']
    crit = [c for c in job['criteria'] if c['name'] in p['decided']['criteria']]
    where = {'title': job['title'], 'hospital': job.get('hospital'), 'slots': job.get('slots'),
             'options': job.get('options')}
    parts = outcome.parts_for(result, '', crit, [], where, p['name'], cfg, now)
    if result == 'next_round' and not parts['slots']:  # the seeded times have passed: list them anyway
        parts['slots'] = outcome.slot_list(job.get('slots'), datetime(2000, 1, 1, tzinfo=UTC))
    parts['drafted_by'] = 'template'
    decision = {'result': result, 'reason': '', 'criteria': [int(c['id']) for c in crit], 'by': DECIDED_BY,
                'at': now.isoformat()}
    status = p['message']
    msg = {'kind': result, 'status': status, 'parts': parts, 'text': outcome.template(parts),
           'approved_by': DECIDED_BY if status in ('approved', 'delivered') else None}
    events = [{'kind': 'decision', 'detail': decision}]
    if status == 'delivered':
        events.append({'kind': 'delivered', 'detail': {'channel': 'call'}})
    out = {'decision': decision, 'message': msg, 'events': row['events'] + events}
    if status == 'delivered':
        out['status'] = outcome.STATUS_AFTER[result]
    if p.get('booked_with') and parts['slots']:
        s = parts['slots'][0]
        out['events'].append({'kind': 'booked', 'detail': {'at': s['at'], 'say': s['say'],
                                                           'with': p['booked_with']}})
    return out


async def seed_cast(sandbox: str, js: list[dict] | None = None) -> int:
    """The guest cast, once per visitor sandbox and never for `live`. One statement, written only when the sandbox
    has no cast yet, so a second run (or two at once) writes nothing. Returns how many people it wrote."""
    if sandbox == 'live':
        return 0
    if await store.has_demo_cast(sandbox, DEMO):
        return 0
    js = js or jobs()[1]
    titles = sorted({p['job'] for p in CAST})
    rows = await octo.query('select j.id, j.title, c.id as criterion_id, c.name from jobs j '
                            'join criteria c on c.job_id = j.id where j.sandbox = $1 and j.title in '
                            '(select jsonb_array_elements_text($2::jsonb)) order by c.id',
                            [sandbox, store._j(titles)])
    ids: dict[str, dict] = {}
    for r in rows:
        j = ids.setdefault(r['title'], {'id': r['id'], 'criteria': {}})
        j['criteria'][r['name']] = r['criterion_id']
    mine = [{**j, 'id': ids[j['title']]['id'],
             'criteria': [{**c, 'id': ids[j['title']]['criteria'].get(c['name'])} for c in j['criteria']]}
            for j in js if j['title'] in ids]
    people = cast(mine)
    return await store.seed_demo_cast(sandbox, DEMO, people) if people else 0


if __name__ == '__main__':
    keys.load()
    p = argparse.ArgumentParser(description='Seed the demo hospitals and jobs into one sandbox.')
    p.add_argument('--sandbox', required=True)
    asyncio.run(seed(p.parse_args().sandbox))
