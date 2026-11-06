"""A pasted job post becomes a draft job: Claude drafts, code keeps only what the post says, the recruiter confirms.

Claude (Haiku 5.5, thinking off) returns the facts, questions and criteria in a fixed JSON shape, each fact and
criterion with the exact words of the post it came from. `ground()` then drops anything whose quote is not in the
post or whose numbers are not in its quote, so pay or a fact the post never stated can't reach Riley. The job is
saved with status 'draft' and goes live only when the recruiter confirms it (`confirm()`).
"""
import json
import re

from riley import store
from riley.store import _j

MODEL = 'claude-haiku-5-5'
COLUMNS = ('location', 'unit', 'shift', 'hours', 'pay')   # facts that also have their own column on jobs
TARGETS = ('years', 'cert', 'license', 'shift', 'unit', 'other')
PROTECTED = re.compile(r'\b(?:age|how old|born|married|marital|children|kids|pregnan\w*|religio\w*|church|'
                       r'disabilit\w*|health conditions?|nationality|salary history|current salary|'
                       r'previous salary)\b', re.I)

PROMPT = """You turn a job post a recruiter pasted into a draft record for her to check before it goes live.

Copy, never invent. Every fact and every criterion carries `quote`: the exact words from the post it comes from, \
copied character for character. If the post doesn't say something (pay, hours, unit, shift, start), leave it out. \
Never guess or estimate pay.

facts: one per thing the post states. Use these keys where they fit: title, hospital, location, unit, shift \
(days, nights, evenings, weekends or per diem), hours (e.g. "3 x 12 h"), pay, start, schedule, benefits, \
sign_on_bonus; another short snake_case key otherwise.

questions: what Riley, a phone screener, asks to check each requirement and preference, plus whether the shift \
works, the earliest start and the hoped-for pay. Reuse these ids where they fit: license, work_authorization, \
years_rn, acute_care, certifications, nights (shift fit, whatever the shift), start_date, pay_expectation. \
about is "person" for facts about the nurse (license, years, units, certifications) and "job" for this role. \
good says what a good answer is (e.g. "at least 2" for years). points: required items 20, others 5 to 15. Never \
ask about age, family, health, religion, national origin or salary history.

criteria: required (the post says required or must), weighted (preferred, counts toward the score) or bonus \
(a plus, nice to have). target_kind is years (target_value a number), cert (e.g. ACLS), license (e.g. NY RN), \
shift, unit or other. points: required 0, weighted 10, bonus 5."""

SCHEMA = {
    'type': 'object', 'additionalProperties': False, 'required': ['facts', 'questions', 'criteria'],
    'properties': {
        'facts': {'type': 'array', 'items': {
            'type': 'object', 'additionalProperties': False, 'required': ['key', 'value', 'quote'],
            'properties': {'key': {'type': 'string'}, 'value': {'type': 'string'}, 'quote': {'type': 'string'}}}},
        'questions': {'type': 'array', 'items': {
            'type': 'object', 'additionalProperties': False, 'required': ['id', 'about', 'ask', 'good', 'points'],
            'properties': {'id': {'type': 'string'}, 'about': {'type': 'string', 'enum': ['person', 'job']},
                           'ask': {'type': 'string'}, 'good': {'type': 'string'}, 'points': {'type': 'integer'}}}},
        'criteria': {'type': 'array', 'items': {
            'type': 'object', 'additionalProperties': False,
            'required': ['kind', 'name', 'points', 'target_kind', 'target_value', 'quote'],
            'properties': {'kind': {'type': 'string', 'enum': ['required', 'weighted', 'bonus']},
                           'name': {'type': 'string'}, 'points': {'type': 'integer'},
                           'target_kind': {'type': 'string', 'enum': list(TARGETS)},
                           'target_value': {'type': 'string'}, 'quote': {'type': 'string'}}}},
    },
}


# ---- rules, without a model ----

def _norm(s: str) -> str:
    s = s.lower().replace('’', "'").replace('‘', "'").replace('“', '"').replace('”', '"')
    return re.sub(r'\s+', ' ', re.sub(r'[–—]', '-', s)).strip()


SPELLED = {w: str(i) for i, w in enumerate(
    ['zero', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine', 'ten', 'eleven', 'twelve'])}


def numbers(s: str) -> set[str]:
    """The numbers in a text, spelled ones too: "Three 12-hour shifts" holds the 3 of "3 x 12 h"."""
    return set(re.findall(r'\d+', s)) | {SPELLED[w] for w in re.findall(r'[a-z]+', s.lower()) if w in SPELLED}


def grounded(post: str, value: str, quote: str) -> bool:
    """The quote is in the post, and every number in the value is in the quote."""
    q = _norm(quote or '')
    return bool(q) and q in _norm(post) and numbers(value) <= numbers(quote)


def _slug(s: str) -> str:
    return re.sub(r'[^a-z0-9]+', '_', s.lower()).strip('_')[:40]


def shift_of(value: str) -> str:
    low = value.lower()
    for word, shift in (('per diem', 'per diem'), ('night', 'nights'), ('evening', 'evenings'),
                        ('weekend', 'weekends'), ('day', 'days')):
        if word in low:
            return shift
    return value


def target(kind: str, value: str) -> dict | None:
    if kind == 'years':
        m = re.search(r'\d+(?:\.\d+)?', value)
        return {'years': float(m.group(0))} if m else None
    if kind == 'shift':
        return {'shift': shift_of(value)}
    return {kind: value.strip()} if kind in ('cert', 'license', 'unit') and value.strip() else None


def ground(post: str, draft: dict) -> tuple[dict, list[dict]]:
    """Keep only what the post says. Returns the clean draft and what was left out, with why, for the recruiter.

    Clean draft: {title, hospital, columns..., facts: {key: value}, quotes: {key: quote}, questions, criteria}."""
    dropped: list[dict] = []
    facts, quotes = {}, {}
    for f in draft.get('facts', []):
        key, value = _slug(f.get('key', '')), (f.get('value') or '').strip()
        if not key or not value:
            continue
        if not grounded(post, value, f.get('quote', '')):
            dropped.append({'fact': key, 'value': value, 'why': 'not in the post'})
        elif key == 'pay' and not numbers(value):
            dropped.append({'fact': key, 'value': value, 'why': 'no amount in the post'})
        elif key not in facts:
            facts[key], quotes[key] = value, f['quote'].strip()
    criteria = []
    for c in draft.get('criteria', []):
        name = (c.get('name') or '').strip()
        if not name:
            continue
        if not grounded(post, f"{name} {c.get('target_value', '')}", c.get('quote', '')):
            dropped.append({'criterion': name, 'why': 'not in the post'})
            continue
        kind = c.get('kind') if c.get('kind') in ('required', 'weighted', 'bonus') else 'weighted'
        tkind, tvalue = c.get('target_kind', 'other'), c.get('target_value', '')
        # "BLS and ACLS" is two items, so matching can say which one is missing
        certs = [p.strip() for p in re.split(r',|/|\band\b', tvalue) if p.strip()] if tkind == 'cert' else []
        for one_name, one_value in [(p, p) for p in certs] if len(certs) > 1 else [(name, tvalue)]:
            criteria.append({'kind': kind, 'name': one_name, 'points': max(0, min(50, int(c.get('points') or 0))),
                             'target': target(tkind, one_value), 'quote': c['quote'].strip()})
    questions, seen = [], set()
    for q in draft.get('questions', []):
        qid, ask = _slug(q.get('id', '')), (q.get('ask') or '').strip()
        if not qid or not ask or qid in seen:
            continue
        if PROTECTED.search(ask):
            dropped.append({'question': qid, 'why': 'asks about a protected topic or salary history'})
            continue
        seen.add(qid)
        questions.append({'id': qid, 'about': 'person' if q.get('about') == 'person' else 'job', 'ask': ask,
                          'good': (q.get('good') or '').strip() or None,
                          'points': max(0, min(50, int(q.get('points') or 0)))})
    clean = {'title': facts.pop('title', None), 'hospital': facts.pop('hospital', None),
             **{c: facts.get(c) for c in COLUMNS}, 'facts': facts, 'quotes': quotes,
             'questions': questions, 'criteria': criteria}
    if clean['shift']:
        clean['shift'] = shift_of(clean['shift'])
    return clean, dropped


# ---- the model and the writes ----

async def draft(post: str) -> tuple[dict, list[dict]]:
    """Claude's draft of the post, grounded. Raises if the model gives nothing usable."""
    from riley.brain import anthropic_client
    reply = await anthropic_client(read_timeout=60.0).messages.create(
        model=MODEL, max_tokens=4000, thinking={'type': 'disabled'}, system=PROMPT,
        messages=[{'role': 'user', 'content': post}],
        output_config={'format': {'type': 'json_schema', 'schema': SCHEMA}})
    if reply.stop_reason != 'end_turn':
        raise RuntimeError(f'the draft stopped early ({reply.stop_reason})')
    text = next((b.text for b in reply.content if b.type == 'text'), '')
    return ground(post, json.loads(text))


async def hospital_id(sandbox: str, name: str | None) -> int | None:
    if not name:
        return None
    rows = await store.octo.query('select id from hospitals where sandbox = $1 and lower(name) = lower($2) limit 1',
                                  [sandbox, name])
    if rows:
        return rows[0]['id']
    return (await store.octo.query('insert into hospitals (sandbox, name) values ($1, $2) returning id',
                                   [sandbox, name]))[0]['id']


async def save(sandbox: str, post: str, clean: dict, dropped: list[dict], source_file: str | None = None) -> dict:
    """The draft job and its criteria, status 'draft'. The post and what was left out go in an event. source_file is
    the name of the PDF or Word file the post came from, when it was uploaded."""
    job = (await store.octo.query(
        "insert into jobs (sandbox, title, location, facts, questions, status, hospital_id, unit, shift, hours, pay, "
        "source_file) "
        "values ($1, $2, $3, $4, $5, 'draft', $6, $7, $8, $9, $10, $11) returning *",
        [sandbox, clean['title'] or 'Untitled role', clean['location'], _j(clean['facts']), _j(clean['questions']),
         await hospital_id(sandbox, clean['hospital']), clean['unit'], clean['shift'], clean['hours'],
         clean['pay'], source_file]))[0]
    rows = []
    for c in clean['criteria']:
        rows += await store.octo.query(
            'insert into criteria (job_id, kind, name, points, target) values ($1, $2, $3, $4, $5) returning *',
            [job['id'], c['kind'], c['name'], c['points'], _j(c['target']) if c['target'] else None])
    await store.log(sandbox, 'job_drafted', detail={'job_id': job['id'], 'post': post[:20000],
                                                    'quotes': clean['quotes'], 'dropped': dropped})
    return {**job, 'criteria': rows, 'dropped': dropped, 'quotes': clean['quotes'],
            'source_file': {'name': source_file} if source_file else None}


async def confirm(sandbox: str, job_id: int, by: str = 'recruiter') -> dict | None:
    """The recruiter checked the draft: it goes live. Only a draft in this sandbox can be confirmed."""
    rows = await store.octo.query(
        "update jobs set status = 'open' where id = $1 and sandbox = $2 and status = 'draft' returning *",
        [job_id, sandbox])
    if rows:
        await store.log(sandbox, 'job_confirmed', detail={'job_id': job_id, 'by': by})
    return rows[0] if rows else None
