"""Matching a person to open jobs, in code over the database (docs/BUILD-PLAN.md, "Matching").

The model never decides a match; it only reads out the list this gives it. What a person has confirmed about
themselves sits in `candidates.facts`:

  {'licenses': ['NY RN'], 'years': 4.0, 'certs': ['ACLS', 'BLS'], 'units': ['telemetry'], 'shifts': ['nights']}

A key that is there is known: a cert not in `certs` is missing. A key that is not there is unknown, and an unknown
required item is never counted as met, so nobody is offered a job on a guess (rule 18).
"""
import re

from . import octo, store

# Words people say for each certification, and for each kind of unit, so "I'm on tele" finds telemetry.
CERTS = {
    'BLS': r'bls|basic life support|cpr', 'ACLS': r'acls|advanced cardiac', 'PALS': r'pals|pediatric advanced',
    'NRP': r'nrp|neonatal resuscitation', 'CCRN': r'ccrn', 'TNCC': r'tncc|trauma nurs\w* core',
    'PCT': r'pct|patient care tech\w*', 'CNA': r'cna|certified nurs\w* assistant', 'EKG': r'ekgs?|ecgs?',
    'phlebotomy': r'phlebotom\w*|blood draws?', 'IV': r'iv', 'fetal monitoring': r'fetal monitor\w*|efm',
    'compact': r'compact|multistate|multi-state',
}
UNITS = {
    'med-surg': r'med[- ]?surg\w*|medical[- ]surgical', 'telemetry': r'tele|telemetry',
    'step-down': r'step[- ]?down|pcu',
    'icu': r'icu|micu|sicu|intensive care|critical care', 'ed': r'\bed\b|\ber\b|emergency',
    'l&d': r'l ?& ?d|l and d|labor and delivery|labou?r', 'peds': r'peds|pediatric\w*|paediatric\w*',
    'rehab': r'rehab\w*', 'long-term care': r'long[- ]term care|nursing home|ltc',
}
SHIFTS = {'days': r'days?|daytime', 'evenings': r'evenings?', 'nights': r'nights?|overnight',
          'weekends': r'weekends?', 'per diem': r'per diem|prn'}
# A clause that says they don't have it, or it has run out: its certs don't count
NOT = re.compile(r"\b(no|not|never|don'?t|doesn'?t|haven'?t|lapsed|expired|expiring|out of date|need to renew)\b",
                 re.I)
# Spoken amounts in "about four years" or "a year and a half"
NUMBERS = {w: i for i, w in enumerate(['zero', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine',
                                       'ten', 'eleven', 'twelve', 'thirteen', 'fourteen', 'fifteen', 'sixteen',
                                       'seventeen', 'eighteen', 'nineteen', 'twenty'])}
NUMBERS.update({'a': 1, 'an': 1, 'thirty': 30, 'forty': 40, 'fifty': 50, 'sixty': 60})


def years_in(answer: str) -> float | None:
    """The years in "about four years", "18 months" or "two and a half years"; None when no amount is said."""
    said = re.search(r'(\d+(?:\.\d+)?|' + '|'.join(NUMBERS) + r')(?:\s+and\s+a\s+half)?\s+(years?|months?)',
                     answer.lower())
    if not said:
        return None
    n = float(said.group(1)) if said.group(1)[0].isdigit() else NUMBERS[said.group(1)]
    n += 0.5 if 'half' in said.group(0) else 0
    return n / 12 if said.group(2).startswith('month') else n


def found(words: dict, text: str) -> list[str]:
    """Which of `words` the answer says they have. A clause with "no", "lapsed" and the like counts for nothing."""
    out = set()
    for clause in re.split(r'[,;.]|\bbut\b|\band\b(?! (?:delivery|d)\b)', text.lower()):
        if NOT.search(clause):
            continue
        out |= {k for k, pat in words.items() if re.search(rf'\b(?:{pat})\b', clause)}
    return sorted(out)


def shifts_of(text: str) -> set[str]:
    """The shifts a job's shift field ("days or evenings") or a caller's words name."""
    return {k for k, pat in SHIFTS.items() if re.search(rf'\b(?:{pat})\b', text.lower())}


def facts_from(questions: list[dict], answers: dict) -> dict:
    """What a screen's confirmed answers say about the person, for `candidates.facts`. Only keys answered on this
    screen are in it, so a merge keeps what an earlier screen found and replaces what this one asked again.
    `other_roles` (their consent) comes back in it too; the store files it under `consent`."""
    facts: dict = {}
    for q in questions:
        a, fact = answers.get(q['id']), q.get('fact')
        if not fact or not a or not a.get('confirmed', True):
            continue
        yes = a.get('meets') == 'yes'
        text = str(a.get('answer', ''))
        if fact == 'years':
            if (n := years_in(text)) is not None and n <= 45:  # more is misheard (screener.years_meet)
                facts['years'] = max(facts.get('years', 0), n)
        elif fact in ('certs', 'units'):
            facts[fact] = sorted({*facts.get(fact, []), *found(CERTS if fact == 'certs' else UNITS, text)})
        elif isinstance(fact, dict):
            (kind, value), = fact.items()
            if kind == 'consent':
                facts[value] = yes
                continue
            key = {'license': 'licenses', 'cert': 'certs', 'shift': 'shifts'}[kind]
            names = sorted(shifts_of(value)) if kind == 'shift' else [value]
            facts[key] = sorted({*facts.get(key, []), *(names if yes else [])})
    return facts


def _item(facts: dict, target: dict) -> tuple[str, float]:
    """One criterion against the facts: ('met' | 'missing' | 'unknown', share of it earned)."""
    (kind, want), = target.items()
    if kind == 'years':
        if 'years' not in facts:
            return 'unknown', 0.0
        have = float(facts['years'])
        return ('met' if have >= want else 'missing'), min(1.0, have / want) if want else 1.0
    if kind == 'units':  # this many kinds of unit
        if 'units' not in facts:
            return 'unknown', 0.0
        n = len(facts['units'])
        return ('met' if n >= want else 'missing'), min(1.0, n / want) if want else 1.0
    key = {'license': 'licenses', 'cert': 'certs', 'unit': 'units', 'shift': 'shifts'}[kind]
    if key not in facts:
        return 'unknown', 0.0
    have = set(facts[key])
    # a shift target like "days or evenings" fits either; a list of certs is any one of them
    wanted = shifts_of(want) if kind == 'shift' else {want} if isinstance(want, str) else set(want)
    ok = bool(wanted & have)
    return ('met', 1.0) if ok else ('missing', 0.0)


def fit(facts: dict, job: dict, criteria: list[dict]) -> dict:
    """How one person fits one job. Required items are each met, missing or unknown; weighted and bonus items earn
    their points, weighted ones in part (1.5 years against 2 earns 75% of it). The job's shift is always a required
    item, so a shift they did not say they can work is never offered. Offered only when every required item is met.
    The lists are sorted, so the order of the criteria never changes the answer."""
    met, missing, unknown = set(), set(), set()
    items = [c for c in criteria if c.get('target')]
    if job.get('shift') and not any('shift' in c['target'] for c in items if c['kind'] == 'required'):
        items.append({'kind': 'required', 'name': job['shift'], 'target': {'shift': job['shift']}})
    got = top = 0.0
    for c in items:
        state, share = _item(facts, c['target'])
        if c['kind'] == 'required':
            {'met': met, 'missing': missing, 'unknown': unknown}[state].add(c['name'])
            continue
        top += c.get('points', 0)
        got += c.get('points', 0) * (share if c['kind'] == 'weighted' else float(state == 'met'))
    return {'offer': not missing and not unknown, 'met': sorted(met), 'missing': sorted(missing),
            'unknown': sorted(unknown), 'credit': round(got / top, 3) if top else 0.0}


def suggest(facts: dict, jobs: list[dict], where: str = '', limit: int = 3) -> list[dict]:
    """For a caller who doesn't know which role: up to `limit` open jobs at the place they named, whose shift they
    said they can work and with no required item they are known to lack. Unknowns are fine here, since the screen
    asks them; the screen decides, not this list."""
    place = where.lower().strip()
    out = []
    for j in jobs:
        f = fit(facts, j, j.get('criteria', []))
        if f['missing']:
            continue
        if place and place not in f"{j.get('hospital', '')} {j.get('borough', '')}".lower():
            continue
        out.append((-f['credit'], j['id'], j))
    return [j for *_, j in sorted(out, key=lambda t: t[:2])[:limit]]


def line(job: dict) -> str:
    """A job as Riley's instructions list it."""
    return f"[{job['id']}] {job['title']} at {job.get('hospital') or 'Hudson Park'}: {job.get('unit')}, " \
           f"{job.get('shift')}, {job.get('hours')}"


# ---- reads and writes ----

async def open_jobs(sandbox: str) -> list[dict]:
    """Every open job in the sandbox with its hospital's name and borough, and its criteria under `criteria`."""
    jobs = await octo.query(
        'select j.*, h.name as hospital, h.borough from jobs j left join hospitals h on h.id = j.hospital_id '
        "where j.sandbox = $1 and j.status = 'open' order by h.name nulls last, j.title", [sandbox])
    crit = await octo.query(
        'select c.* from criteria c join jobs j on j.id = c.job_id '
        "where j.sandbox = $1 and j.status = 'open' order by c.id", [sandbox])
    # Octo sends bigints as text; ids are numbers here, so choose_role(job_id) and exclude_job_id compare
    for c in crit:
        c.update(id=int(c['id']), job_id=int(c['job_id']), points=int(c['points'] or 0),
                 target=store._j_load(c['target']))
    for j in jobs:
        j['id'] = int(j['id'])
        j['criteria'] = [c for c in crit if c['job_id'] == j['id']]
    return jobs


async def offers(sandbox: str, candidate_id: int, exclude_job_id: int | None = None) -> list[dict]:
    """The other roles this person can be offered: open jobs where every required item is met by what they have
    confirmed, and only if they agreed to be considered for other roles. Best fit first."""
    rows = await octo.query('select facts, consent from candidates where id = $1 and sandbox = $2',
                            [candidate_id, sandbox])
    if not rows or not (store._j_load(rows[0]['consent']) or {}).get('other_roles'):
        return []
    facts = store._j_load(rows[0]['facts']) or {}
    skip = int(exclude_job_id) if exclude_job_id is not None else None
    fits = [(fit(facts, j, j['criteria']), j) for j in await open_jobs(sandbox) if j['id'] != skip]
    return [{'job_id': j['id'], 'title': j['title'], 'hospital': j.get('hospital'), 'unit': j.get('unit'),
             'shift': j.get('shift')}
            for f, j in sorted(fits, key=lambda t: (-t[0]['credit'], t[1]['id'])) if f['offer']]


async def after_screen(sandbox: str, candidate_id: int, questions: list[dict], answers: dict) -> list[dict]:
    """After a screen is saved: what it said about the person goes into their facts, and every open job they now
    fully qualify for is written as one `match` event. Returns those matches."""
    facts = facts_from(questions, answers)
    merged = await store.save_facts(candidate_id, facts)
    matches = []
    for j in await open_jobs(sandbox):
        f = fit(merged, j, j['criteria'])
        if f['offer']:
            matches.append({'job_id': j['id'], 'title': j['title'], 'hospital': j.get('hospital'),
                            'credit': f['credit']})
            await store.log(sandbox, 'match', candidate_id, None, matches[-1])
    return matches
