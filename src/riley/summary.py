"""Riley's written summary of one screen, for the top of Dana's sheet and her Next up row (issue #87).

Claude Haiku reads one application's screen record (each answer with where it came from, required items met or not,
corrections, what the candidate asked, notes for the recruiter, Dana's decision) and writes a decision line, two to
four short lines on what stands out and what Dana might check, and a one-line version for the row. Structured output
(SCHEMA), so the console renders fields, never free text.

The rules are in the prompt and again in `problems()`: every line names the facts it rests on (`refs`) and its words
and numbers must be in them, or the line is dropped; no score, ranking or comparison with other people; nothing about
how someone sounds; no protected topic (screen.yaml rules.protected_topics); never a suggestion to reject; no em
dashes; short. Answers that touch a protected topic are taken out of the record before the model sees them.

Each summary is kept in the `summaries` table with the application's own `sandbox` (the tenant key, decision 0022)
and a fingerprint of the record it was written from. The tracker serves it only while the fingerprint still matches,
so a changed answer, a correction or a decision gets a new one; until then, and whenever there is no key or the model
fails, the console shows its rule-built lines.
"""
import asyncio
import hashlib
import json
import os
import re
import time
from typing import Any

from . import octo, store

PROMPT_VERSION = 2
LIMITS = {'decision': 120, 'line': 120, 'row': 80}
TONES = {'met': 'ok', 'not_met': 'danger', 'check': 'warn', 'asked': 'riley', 'note': 'muted'}
RESULT_WORD = {'next_round': 'Interview', 'not_yet': 'Not yet', 'not_selected': 'Not now'}
PER_HOUR = int(os.environ.get('RILEY_SUMMARIES_PER_HOUR', '300'))  # a cap on spend per web process
RETRY_AFTER_S = 3600  # a failed record is tried again after this long, not on every poll

SYSTEM = """You write a short summary of one nurse applicant's phone screen for Dana, the hospital recruiter who \
decides. You get the screen record as JSON: a list of facts, each with a "ref". Write in plain recruiter words, \
short and specific, quoting the candidate's own answer words where they matter. Write to Dana as "you".

Write:
- "decision": one sentence that puts the decision to Dana, using the person's first name and the "required items" \
fact as it is, for example "Nina meets 5 of 6 required items. What do you want to tell them?". When Dana answered \
already, say what you answered, for example "You answered Not yet for Theo.".
- "lines": 2 to 4 lines on what stands out and what Dana might want to check: a required item not met (quote the \
answer), an answer that changed on the call, an answer from the resume not confirmed yet, a resume that said it in \
other words than the job needs, an unclear, missing or declined answer, what the candidate asked, a note for the \
recruiter. When every required item is met and nothing else stands out, say that in one line and add what they \
said about their start date, shift or experience. Do not spend a line listing answers that are simply met. One \
point per line. Give each line a tone: met, not_met, check, asked or note.
- "row": one phrase of at most 70 characters for Dana's list, the one thing that stands out most.

Rules, all of them strict:
- Every line lists in "refs" the refs of the facts it rests on, and says nothing those facts do not say. No guesses, \
no inferences about the person, no advice beyond "check" or "ask".
- Never a score, percent, points, rank or rating, and never compare them with other candidates or applicants.
- Never describe how the person sounded, their tone, voice, accent, mood or confidence.
- Never mention age, date of birth, race, ethnicity, religion, national origin, citizenship beyond work \
authorization, marital status, pregnancy, children or childcare, family, disability or health conditions, sexual \
orientation, gender identity, arrests or salary history at past jobs, even if the record seems to touch one.
- Never suggest rejecting them, not moving forward, or answering Not yet or Not now. Dana decides.
- Say "on the call" or "confirmed" only for answers whose source is "said on the call"; an answer "from the \
resume, not confirmed yet" is from the resume.
- An answer to a question is what they answered, not something they asked for: "Yes" to being considered for \
other roles means they are open to them.
- Never say what the role does or does not require beyond each question's "required" flag.
- Use the first name or "they", never he, she, him, her, his or hers.
- No dates or times (the console shows the promised date), no em dashes, no lists inside a line.
- decision under 110 characters, each line under 90.

Screen record (JSON):
"""

_TEXT = {'type': 'object', 'additionalProperties': False, 'required': ['text', 'refs'],
         'properties': {'text': {'type': 'string'}, 'refs': {'type': 'array', 'items': {'type': 'string'}}}}
SCHEMA = {
    'type': 'object', 'additionalProperties': False, 'required': ['decision', 'lines', 'row'],
    'properties': {
        'decision': _TEXT,
        'lines': {'type': 'array', 'items': {
            'type': 'object', 'additionalProperties': False, 'required': ['text', 'tone', 'refs'],
            'properties': {'text': {'type': 'string'}, 'tone': {'type': 'string', 'enum': list(TONES)},
                           'refs': {'type': 'array', 'items': {'type': 'string'}}}}},
        'row': _TEXT,
    },
}

# ---- what may never be said ----

# Words for each of screen.yaml's protected topics, as they show up in a recruiter's summary. A topic added to
# screen.yaml with no words here is still matched by its own name.
TOPIC_WORDS = {
    'age': r'\bage[ds]?\b|years? old|\bolder\b|\byounger\b|\belderly\b|\bretire\w*|\bI(\'m| am) \d{2}\b',
    'date of birth': r'\bbirth\w*|\bborn\b|\bbirthday',
    'race': r'\brace\b|\bracial\w*|\bethnic\w*|\bskin\b|\bwhite\b|\bblack\b|\bhispanic|\blatin[oax]\b|\basian\b',
    'religion': r'\breligio\w*|\bchurch|\bmosque|\bsynagogue|\btemple\b|\bfaith\b|\bpray\w*|\bsabbath|\bhalal|\bkosher',
    'national origin': r'\bimmigra\w*|\bforeign\w*|\baccent\w*|\bnationality|\bnative\b|\bhome country|\bborn in',
    'citizenship beyond work authorization': r'\bcitizen\w*|\bgreen card|\bvisa\b|\bundocumented',
    'marital status': r'\bmarri\w*|\bdivorc\w*|\bhusband|\bwife\b|\bspouse|\bwidow\w*|\bsingle mom|\bpartner\'s',
    'pregnancy': r'\bpregnan\w*|\bmaternity|\bexpecting a|\bbaby\b',
    'children or childcare': r'\bkids?\b|\bchild\w*|\bson\b|\bdaughter|\bdaycare|\bbabysit\w*|\bparent\w*|'
                             r'\bfamily|\bfamilies',
    'disability or health conditions': r'\bdisab\w*|\bdiagnos\w*|\billness|\bsick\b|\bsurgery|\binjur\w*|'
                                       r'\bhealth (condition|issue|problem)s?|\bmedical (condition|leave|issue)s?|'
                                       r'\bmental health|\btherapy|\bmedication|\bwheelchair|\bchronic',
    'sexual orientation': r'\bgay\b|\blesbian|\bbisexual|\bqueer|\bsexual\w*',
    'gender identity': r'\bgender\w*|\btransgender|\bnon-?binary|\bpronoun\w*',
    'arrests': r'\barrest\w*|\bcriminal|\bconvict\w*|\bjail\b|\bprison|\bfelon\w*|\bcourt\b',
    'salary history at past jobs': r'\bsalary history|\b(current|previous|past|last|old) (pay|salary|wage|rate)|'
                                   r'\bused to (make|earn|get paid)|\bwas (paid|making|earning)|'
                                   r'\b(made|make|earned|earn|get|got) \$?\d[^.]*\b(last|previous|old|current|past) '
                                   r'(job|employer|position|hospital|place)',
}
SCORE = re.compile(r'\bscor\w*|\bpercent\w*|%|\bpoints?\b|\brank\w*|\brating\b|\brated\b|\bout of 100|\bgrade[ds]?\b|'
                   r'\b(best|strongest|weakest|stronger|weaker)\b|\b(better|worse) than\b|\bcompar\w*|'
                   r'\bother (candidates|applicants|people|nurses)|\bthe rest\b|\baverage\b|\bstands? above', re.I)
SOUND = re.compile(r'\bsound\w*|\btone\b|\bvoice\b|\baccent\w*|\benthusias\w*|\bconfiden\w*|\bnervous\w*|'
                   r'\bhesita\w*|\bfriendly\b|\benerget\w*|\barticulate\b|\bmumbl\w*|\bupbeat\b|\bcalm\b|'
                   r'\banxious\w*|\bexcited\b|\beager\w*|\bwarm\b|\bpolite\b|\brude\b|\bprofessional\b|'
                   r'\bpersonality\b|\battitude\b|\bmood\b|\bseemed\b|\bseems\b|\bimpression', re.I)
REJECT = re.compile(r'\breject\w*|\bdisqualif\w*|\brule (them|it) out|\bscreen (them )?out|'
                    r'\b(they|(?-i:[A-Z][a-z]+)) (is|are|isn\'t|aren\'t)( not)? (a )?(good |great )?fit\b|'
                    r'\bpoor fit|\bbad fit|\bpass on\b|\bturn (them )?down|'
                    r'\b(not|don\'t|do not|no need to) (move|moving) '
                    r'(forward|ahead|on)|\bunsuitable|\bunqualified|\bnot qualified|\bshould not\b|\bshouldn\'t\b|'
                    r'\b(say|send|choose|pick|answer|go with|recommend\w*|suggest\w*|consider)\s+(a\s+)?'
                    r'(not yet|not now|no)\b|\bdecline (them|the)\b|\bnot worth\b', re.I)
PRONOUN = re.compile(r'\b(he|she|him|her|his|hers|himself|herself)\b', re.I)
DASH = re.compile(r'\s*[—–]\s*')
_NUMBERS = 'zero one two three four five six seven eight nine ten eleven twelve'
NUMBER_WORDS = {w: str(i) for i, w in enumerate(_NUMBERS.split())}
# Words a summary may use that no answer needs to contain: how a recruiter talks about a screen
_COMMON = """a an the and or but if then so to of in on at by for from with without into as is are was were be
been being has have had do does did not no nor yes it its this that these those there their they them theirs what
which who when where why how all any each every both some one ones only also still yet just more most less than very
about after again before up down out over under can could may might must will would should need needs needed want
wants wanted like said says say told tell asked ask asks asking answer answers answered answering question questions
call called calls calling phone screen screened screening resume resumes record recorded confirmed confirm confirms
confirming unconfirmed check checked checking clarify follow item items required require requires requirement
requirements met meet meets meeting missing unclear declined decline declines declining given give gave open job role
roles post posting interview interviews invite invited next step steps round answer you your dana riley recruiter
person candidate nurse change changed changes changing earlier later first then now late due reply decide decision
decided worth whether hear heard note notes noted mention mentioned mentions says wrote other words different
differently same held holds hold current currently active work works working worked experience years year already
keep chose choose chosen move moved moving forward come comes look looks review reviewed pay rate hoping hope hopes
date expectation start shift shifts
both plus ready fine okay ok sure enough left something anything nothing everything well good clear clearly"""
COMMON = frozenset(_COMMON.split())
# Short forms a recruiter writes for words in the record, as `_norm` reads them
ALIASES = {'ny': 'york', 'nys': 'york', 'us': 'state', 'usa': 'state', 'yrs': 'years', 'yr': 'year', 'hrs': 'hours',
           'hr': 'hour', 'wks': 'weeks', 'wk': 'week', 'mos': 'month', 'certs': 'certi', 'cert': 'certi',
           'med-surg': 'med-s', 'medsurg': 'med-s'}
WORD = re.compile(r"[A-Za-z][A-Za-z'+-]*|\d+(?:\.\d+)?")


def protected(cfg: dict) -> re.Pattern:
    """One pattern for every protected topic in screen.yaml, with TOPIC_WORDS for the ones it knows."""
    topics = (cfg.get('rules') or {}).get('protected_topics') or list(TOPIC_WORDS)
    parts = [TOPIC_WORDS.get(t) or re.escape(t) for t in topics]
    return re.compile('|'.join(f'(?:{p})' for p in parts), re.I)


# ---- the record ----

def _source(check: dict) -> str:
    src = check.get('source') or 'call'
    if src == 'resume':
        return 'from the resume, ' + ('confirmed on the call' if check.get('confirmed') else 'not confirmed yet')
    return 'said on the call' if src == 'call' else str(src)


def record(name: str, title: str, status: str, checks: list[dict], decision: dict | None = None,
           asked: list[str] | None = None, notes: list[str] | None = None, cfg: dict | None = None) -> dict:
    """One application's screen record as the model reads it: a list of facts, each with a ref. `checks` are the
    tracker's items (web/tracker/rules.items). Any answer that touches a protected topic is replaced before it goes."""
    bad = protected(cfg or {})

    def safe(text) -> str | None:
        if text is None:
            return None
        text = str(text).strip()
        return '[a personal detail, left out]' if bad.search(text) else text

    facts = []
    for c in checks:
        fact = {'ref': f"a:{c['id']}", 'kind': 'answer', 'question': c.get('ask') or c['id'],
                'required': bool(c.get('required')), 'answer': safe(c.get('answer')),
                'meets': c.get('meets') or ('not answered yet' if c.get('answer') is None else 'unclear')}
        if c.get('answer') is not None:
            fact['source'] = _source(c)
        if c.get('earlier'):
            fact['said_earlier_on_the_call'] = [safe(e) for e in c['earlier'] if e]
        if c.get('resume') and c.get('needs'):
            fact |= {'resume_said': safe(c['resume']), 'job_needs': c['needs']}
        facts.append(fact)
    facts += [{'ref': f'q:{i}', 'kind': 'candidate asked', 'text': safe(q)}
              for i, q in enumerate(dict.fromkeys(q for q in asked or [] if q), 1)]
    facts += [{'ref': f'n:{i}', 'kind': 'note for the recruiter', 'text': safe(n)}
              for i, n in enumerate(dict.fromkeys(n for n in notes or [] if n), 1)]
    required = [c for c in checks if c.get('required')]
    if required:
        met = sum(c.get('meets') == 'yes' for c in required)
        facts.append({'ref': 'r', 'kind': 'required items',
                      'text': f'{met} of {len(required)} required items met'
                              + (f", {len(required) - met} not met or not known" if met < len(required) else '')})
    if decision and decision.get('result'):
        facts.append({'ref': 'd', 'kind': 'your decision',
                      'text': f"Dana answered {RESULT_WORD.get(decision['result'], decision['result'])}"
                              + (f": {safe(decision['reason'])}" if decision.get('reason') else '')})
    return {'person': (name or '').split(' ')[0] or 'They', 'role': title, 'status': status, 'facts': facts}


def fingerprint(rec: dict) -> str:
    """What the summary was written from. A different answer, correction, question or decision is a new one."""
    blob = json.dumps({'v': PROMPT_VERSION, 'record': rec}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


# ---- the checks every line passes ----

def _norm(token: str) -> str:
    t = token.lower().strip("'+-")
    t = ALIASES.get(t, NUMBER_WORDS.get(t, t))
    return t[:5] if len(t) > 5 and not t.isdigit() else t


def _fact_text(fact: dict) -> str:
    out = []
    for k, v in fact.items():
        if k in ('ref', 'kind', 'required'):
            continue
        out += [str(x) for x in v] if isinstance(v, list) else [str(v)]
    return ' '.join(out)


def counts(rec: dict) -> set[str]:
    """The numbers a line may give that no answer holds: how many answers changed or are open, and how many
    things they asked. (How many required items are met is a fact of its own, ref "r".)"""
    answers = [f for f in rec['facts'] if f['kind'] == 'answer']
    numbers = {sum(bool(f.get('said_earlier_on_the_call')) for f in answers),
               sum(f['meets'] not in ('yes', 'no') for f in answers),
               sum(f['kind'] == 'candidate asked' for f in rec['facts'])}
    return {str(n) for n in numbers if n}


def problems(text: str, refs: list[str], rec: dict, cfg: dict, limit: int) -> list[str]:
    """Why a line may not be shown, or [] when it may. Its refs must be facts in the record and its words and
    numbers must come from them (or from the few words any summary uses); the rest are the house rules."""
    out = []
    by_ref = {f['ref']: f for f in rec['facts']}
    if not text.strip():
        return ['empty']
    if len(text) > limit:
        out.append(f'longer than {limit}')
    if not refs:
        out.append('no refs')
    unknown = [r for r in refs if r not in by_ref]
    if unknown:
        out.append(f'unknown refs {unknown}')
    for name, pattern in (('score or comparison', SCORE), ('how they sound', SOUND), ('rejects', REJECT),
                          ('gendered pronoun', PRONOUN), ('protected topic', protected(cfg)), ('em dash', DASH)):
        if m := pattern.search(text):
            out.append(f'{name}: "{m.group().strip()}"')
    source = ' '.join([rec.get('person') or '', rec.get('role') or '']
                      + [_fact_text(by_ref[r]) for r in refs if r in by_ref])
    have = {_norm(t) for t in WORD.findall(source)} | counts(rec)
    words = [t for t in WORD.findall(text) if _norm(t) not in COMMON and t.lower() not in COMMON]
    missing = [t for t in words if _norm(t) not in have]
    numbers = [t for t in missing if _norm(t).isdigit()]
    names = [t for t in missing if sum(ch.isupper() for ch in t) >= 2]
    if numbers:
        out.append(f'numbers not in the record {numbers}')
    if names:
        out.append(f'names not in the record {names}')
    if words and len(missing) > max(1, len(words) // 4):
        out.append(f'words not in the record {missing}')
    return out


def clean(raw: dict, rec: dict, cfg: dict) -> tuple[dict | None, list[dict]]:
    """The model's answer as the console may show it, and what was dropped and why. None when fewer than two lines
    survive: the console then shows its own lines. Em dashes become commas before anything is checked."""
    dropped = []

    def fix(text) -> str:
        return DASH.sub(', ', str(text or '')).strip()

    def keep(part: str, item: dict | None, limit: int) -> dict | None:
        if not isinstance(item, dict):
            return None
        text, refs = fix(item.get('text')), [str(r) for r in item.get('refs') or []]
        why = problems(text, refs, rec, cfg, limit)
        if why:
            dropped.append({'part': part, 'text': text, 'why': why})
            return None
        return {'text': text, 'refs': refs}

    lines = []
    for item in (raw.get('lines') or [])[:4]:
        line = keep('line', item, LIMITS['line'])
        if line:
            lines.append({**line, 'tone': TONES.get(str(item.get('tone')), 'muted')})
    if len(lines) < 2:
        return None, dropped
    return {'decision': keep('decision', raw.get('decision'), LIMITS['decision']), 'lines': lines,
            'row': keep('row', raw.get('row'), LIMITS['row'])}, dropped


# ---- writing one ----

def model_of(cfg: dict) -> str | None:
    """screen.yaml dana.summary_model (else dana.draft_model), when it is an Anthropic model and the key is here."""
    dana = cfg.get('dana') or {}
    spec = dana.get('summary_model') or dana.get('draft_model') or {}
    if spec.get('provider') != 'anthropic' or not os.environ.get('ANTHROPIC_API_KEY'):
        return None
    return spec.get('model')


def light_client(read_timeout: float) -> Any:
    """The same client as agent.brain.anthropic_client (the workspace header our key needs, no retries), without
    importing agent.brain: the web container has 256 MB, and the LiveKit plugins brain loads do not fit beside it."""
    import anthropic
    import httpx2
    headers = {}
    if os.environ.get('ANTHROPIC_WORKSPACE_ID', '').startswith('wrkspc_'):
        headers['anthropic-workspace-id'] = os.environ['ANTHROPIC_WORKSPACE_ID']
    return anthropic.AsyncClient(
        api_key=os.environ['ANTHROPIC_API_KEY'], max_retries=0, default_headers=headers,
        http_client=httpx2.AsyncClient(timeout=httpx2.Timeout(5.0, read=read_timeout), follow_redirects=True))


async def write(rec: dict, cfg: dict, wait_s: float = 20) -> tuple[dict | None, dict]:
    """(summary or None, how): the model's summary after `clean`, with the model, tokens, seconds, what was dropped
    and any error in `how`. Never raises."""
    model = model_of(cfg)
    if not model:
        return None, {'error': 'no key'}
    started = time.monotonic()
    how: dict = {'model': model}
    try:
        client = light_client(read_timeout=wait_s)
        thinking: Any = {'type': 'disabled'} if 'haiku' in model else {'type': 'adaptive'}  # a summary needs no think
        reply = await asyncio.wait_for(client.messages.create(
            model=model, max_tokens=1200,
            messages=[{'role': 'user', 'content': SYSTEM + json.dumps(rec, ensure_ascii=False)}],
            thinking=thinking,
            output_config={'effort': 'low', 'format': {'type': 'json_schema', 'schema': SCHEMA}}), wait_s + 5)
        how['usage'] = {'input': reply.usage.input_tokens, 'output': reply.usage.output_tokens}
        if reply.stop_reason != 'end_turn':
            raise ValueError(f'stop {reply.stop_reason}')
        raw = json.loads(next(b.text for b in reply.content if b.type == 'text'))
    except Exception as e:  # no summary this time; the console keeps its own lines
        how |= {'error': f'{type(e).__name__}: {str(e)[:200]}', 'seconds': round(time.monotonic() - started, 2)}
        return None, how
    how['seconds'] = round(time.monotonic() - started, 2)
    summary, how['dropped'] = clean(raw, rec, cfg)
    if summary is None:
        how['error'] = 'fewer than two lines passed the checks'
    return summary, how


# ---- kept in Octo, beside the application ----

_RUNNING: set[int] = set()
_TASKS: set[asyncio.Future] = set()
_SPENT: list[float] = []
_GATE = asyncio.Semaphore(2)


async def stored(scope: list[str], ids: list[int]) -> dict[int, dict]:
    """The summaries of these applications, only from the sandboxes this tenant reads."""
    if not ids:
        return {}
    rows = await octo.query('select application_id, sandbox, fingerprint, summary, how, at from summaries '
                            'where sandbox = any($1) and application_id = any($2::bigint[])', [scope, ids],
                            row_limit=2000)
    return {int(r['application_id']): r for r in rows}


async def save(application_id: int, sandbox: str, fp: str, summary: dict | None, how: dict) -> None:
    """One application's summary. The row is only ever written under the application's own sandbox."""
    await octo.query('insert into summaries (application_id, sandbox, fingerprint, summary, how, at) '
                     'select a.id, a.sandbox, $3::text, $4::jsonb, $5::jsonb, now() from applications a '
                     'where a.id = $1 and a.sandbox = $2 '
                     'on conflict (application_id) do update set sandbox = excluded.sandbox, '
                     'fingerprint = excluded.fingerprint, summary = excluded.summary, how = excluded.how, at = now()',
                     [application_id, sandbox, fp, json.dumps(summary) if summary else None, json.dumps(how)])


def _budget_left() -> bool:
    now = time.time()
    _SPENT[:] = [t for t in _SPENT if now - t < 3600]
    return len(_SPENT) < PER_HOUR


async def _refresh(application_id: int, sandbox: str, rec: dict, fp: str, cfg: dict) -> None:
    try:
        async with _GATE:
            summary, how = await write(rec, cfg)
            if how.get('error') == 'no key':
                return
            await save(application_id, sandbox, fp, summary, how)
    except Exception as e:  # a failed save only means the next poll tries again
        print('summary save failed', application_id, type(e).__name__, e, flush=True)
    finally:
        _RUNNING.discard(application_id)


def _due(row: dict | None, fp: str) -> bool:
    if row is None or row['fingerprint'] != fp:
        return True
    if row.get('summary') is not None:
        return False
    at = row.get('at')
    when = store.ts(at)
    return when is None or time.time() - when.timestamp() > RETRY_AFTER_S


def shown(row: dict | None, fp: str) -> dict | None:
    """What the console gets: the stored summary while it is still for this record, else None."""
    if not row or row['fingerprint'] != fp or row.get('summary') is None:
        return None
    s = octo_json(row['summary'])
    return {**s, 'model': (octo_json(row.get('how')) or {}).get('model')}


def octo_json(v):
    return json.loads(v) if isinstance(v, str) else v


async def for_applications(scope: list[str], wanted: list[tuple[int, str, dict]], cfg: dict) -> dict[int, dict]:
    """{application id: summary} for the ones with a current summary. Each one missing or out of date is written in
    the background (two at a time, at most PER_HOUR an hour), so it shows on the next poll. `wanted` is (id, its
    sandbox, its record)."""
    if not wanted:
        return {}
    have = await stored(scope, [i for i, _, _ in wanted])
    out = {}
    can_write = model_of(cfg) is not None
    for application_id, sandbox, rec in wanted:
        fp = fingerprint(rec)
        row = have.get(application_id)
        if (s := shown(row, fp)) is not None:
            out[application_id] = s
        if (can_write and _due(row, fp) and application_id not in _RUNNING and sandbox in scope
                and _budget_left()):
            _RUNNING.add(application_id)
            _SPENT.append(time.time())
            task = asyncio.ensure_future(_refresh(application_id, sandbox, rec, fp, cfg))
            _TASKS.add(task)
            task.add_done_callback(_TASKS.discard)
    return out
