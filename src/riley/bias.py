"""The bias check: how often each self-identified group moves forward, per job, under the four-fifths rule.

Candidates may give the standard EEO answers (sex, race or ethnicity, or "Decline to say") on a short optional form
after a screen. They sit in `self_id` and only this module reads them (SPEC rules 11 and 12): Riley, the scorer,
matching and Dana's candidate view never do. A group whose selection rate is below 80% of the highest group's is
flagged as possible adverse impact, and that job switches to "Dana confirms every no". This is the data the
independent audit under NYC Local Law 144 needs before real use (docs/BUILD-PLAN.md, "The bias check").

The rules are plain functions; `check` and `seed_demo` are the only ones that touch Octo, through riley/store.py.
"""
import base64
import hashlib
import hmac
import os
from fractions import Fraction

from . import store

MIN_GROUP = 5             # fewer people than this in a group: too few to compare, and no counts shown
RATIO = Fraction(4, 5)    # the four-fifths rule
DECLINE = 'Decline to say'
SELECTED = 'next_round'   # moving forward means Dana's decision was next round

# The standard EEO self-identification questions, in the order the form asks them
QUESTIONS = {
    'sex': {'label': 'Sex', 'options': ['Female', 'Male', DECLINE]},
    'race': {'label': 'Race or ethnicity', 'options': [
        'Hispanic or Latino', 'White', 'Black or African American', 'Asian',
        'Native Hawaiian or Other Pacific Islander', 'American Indian or Alaska Native', 'Two or more races',
        DECLINE]},
}


def clean(answers: dict) -> dict:
    """Only the listed questions and answers, so nothing free-typed is ever stored."""
    return {q: answers[q] for q in QUESTIONS if answers.get(q) in QUESTIONS[q]['options']}


def rates(rows: list[dict]) -> dict[str, dict]:
    """Selection rate per group from rows of {group, selected}. No answer and "Decline to say" are in no group."""
    out: dict[str, dict] = {}
    for r in rows:
        g = r.get('group')
        if not g or g == DECLINE:
            continue
        s = out.setdefault(g, {'n': 0, 'selected': 0})
        s['n'] += 1
        s['selected'] += bool(r['selected'])
    for s in out.values():
        s['rate'] = round(s['selected'] / s['n'], 3)
    return out


def _exact(r: dict) -> Fraction:
    return Fraction(r['selected'], r['n'])


def _top(rates: dict) -> Fraction:
    # The highest rate among groups big enough to compare: one person picked of one is not a 100% bar
    return max((_exact(r) for r in rates.values() if r['n'] >= MIN_GROUP), default=Fraction(0))


def four_fifths(rates: dict) -> list[str]:
    """Groups whose selection rate is below 80% of the highest group's: possible adverse impact. Exact fractions,
    so a rate of exactly 80% is never flagged by rounding."""
    top = _top(rates)
    return sorted(g for g, r in rates.items() if r['n'] >= MIN_GROUP and _exact(r) < RATIO * top)


def too_few(rates: dict) -> list[str]:
    return sorted(g for g, r in rates.items() if 0 < r['n'] < MIN_GROUP)


def report(rows: list[dict], jobs: list[dict]) -> list[dict]:
    """Per job and question: each group's rate and the four-fifths result. Only counts leave here, and none for a
    group under MIN_GROUP, so no page or export shows one person's answers."""
    out = []
    for job in jobs:
        mine = [r for r in rows if r['job_id'] == job['id']]
        questions, flagged = [], {}
        for q, spec in QUESTIONS.items():
            r = rates([{'group': (row['answers'] or {}).get(q), 'selected': row['selected']} for row in mine])
            flags, top = four_fifths(r), _top(r)
            if flags:
                flagged[q] = flags
            groups = [{'group': g, 'n': s['n'], 'selected': s['selected'], 'rate': s['rate'],
                       'ratio': round(float(_exact(s) / top), 3) if top else None, 'flagged': g in flags}
                      if s['n'] >= MIN_GROUP else {'group': g, 'too_few': True}
                      for g, s in sorted(r.items(), key=lambda kv: spec['options'].index(kv[0])
                                         if kv[0] in spec['options'] else 99)]
            questions.append({'question': q, 'label': spec['label'], 'groups': groups, 'flagged': flags})
        out.append({'job_id': job['id'], 'title': job['title'], 'hospital': job.get('hospital'),
                    'referral_points': bool(job.get('referral_points')),
                    'confirm_every_no': bool(job.get('confirm_every_no')) or bool(flagged),
                    'decided': len(mine), 'questions': questions, 'flagged': bool(flagged),
                    'flagged_groups': flagged})
    return out


def needs_dana(job: dict, result: str) -> bool:
    """On a job in "Dana confirms every no", no automatic no goes out: she confirms each one (riley/outcome.py)."""
    return bool(job.get('confirm_every_no')) and result != SELECTED


# ---- the self-id link: a token that names the person without showing their id or number ----

def _secret() -> bytes:
    raw = os.environ.get('SELF_ID_SECRET') or os.environ.get('NURSE_OCTO_KEY') or 'dev only'
    return hashlib.sha256(b'self-id:' + raw.encode()).digest()


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip('=')


def token(sandbox: str, candidate_id: int) -> str:
    """For /self-id?token=...: the page after a screen and Dana's email link to it."""
    body = f'{sandbox}|{candidate_id}'.encode()
    return f'{_b64(body)}.{_b64(hmac.new(_secret(), body, hashlib.sha256).digest()[:18])}'


def read_token(t: str) -> tuple[str, int] | None:
    try:
        body_b64, sig = t.split('.')
        body = base64.urlsafe_b64decode(body_b64 + '=' * (-len(body_b64) % 4))
        if not hmac.compare_digest(sig, _b64(hmac.new(_secret(), body, hashlib.sha256).digest()[:18])):
            return None
        sandbox, cid = body.decode().rsplit('|', 1)
        return sandbox, int(cid)
    except (ValueError, UnicodeDecodeError):
        return None


# ---- with the database ----

async def check(sandbox: str) -> list[dict]:
    """The report for a sandbox. A newly flagged job switches to "Dana confirms every no", with one event that holds
    only the job and the group names."""
    jobs = await store.bias_jobs(sandbox)
    out = report(await store.bias_rows(sandbox), jobs)
    was = {j['id']: bool(j.get('confirm_every_no')) for j in jobs}
    for j in out:
        if j['flagged'] and not was[j['job_id']]:
            await store.set_confirm_every_no(j['job_id'])
            await store.log(sandbox, 'bias_flag', detail={'job_id': j['job_id'], 'flagged': j['flagged_groups'],
                                                          'set': 'dana confirms every no'})
    return out


async def confirms_every_no(sandbox: str, job_id) -> bool:
    """For `outcome.auto()` before an automatic no: whether this job is in "Dana confirms every no", after a fresh
    check, so a job that crossed the line with the last decision is held too. Only the yes or no leaves here."""
    return any(str(j['job_id']) == str(job_id) and j['confirm_every_no'] for j in await check(sandbox))


# Made-up people for a visitor's sandbox, so the page has something to show: the first job has a gap the rule
# flags, the second does not. (group, sex, people, of them moved forward)
_DEMO = [
    [('White', 'Female', 8, 6), ('Black or African American', 'Female', 8, 3), ('Hispanic or Latino', 'Male', 6, 4),
     ('Asian', 'Male', 3, 2), (DECLINE, DECLINE, 2, 1)],
    [('White', 'Male', 6, 3), ('Black or African American', 'Female', 6, 3), ('Hispanic or Latino', 'Female', 5, 3),
     ('Asian', 'Female', 2, 1)],
]
_NAMES = ['Avery Lane', 'Jordan Pike', 'Casey Moreno', 'Riley Shaw', 'Morgan Tate', 'Quinn Ellis', 'Drew Hollis',
          'Jamie Ortega', 'Rowan Price', 'Sage Whitley', 'Emerson Cole', 'Parker Dunn']


def demo_people(job_ids: list[int]) -> list[dict]:
    people = []
    for job_id, groups in zip(job_ids, _DEMO, strict=False):
        for race, sex, n, sel in groups:
            for i in range(n):
                k = len(people)
                people.append({'key': f'demo-bias-{k}@example.invalid', 'name': _NAMES[k % len(_NAMES)],
                               'job_id': job_id, 'result': SELECTED if i < sel else 'not_selected',
                               'answers': {'race': race, 'sex': sex}})
    return people


async def seed_demo(sandbox: str, cfg: dict) -> None:
    """Once per visitor sandbox, never for `live`: made-up decided applications with made-up self_id answers."""
    if sandbox == 'live' or await store.has_self_id(sandbox):
        return
    ids = [j['id'] for j in await store.bias_jobs(sandbox)][:2] or [(await store.job_for(sandbox, cfg))['id']]
    await store.seed_bias_demo(sandbox, demo_people(ids))
