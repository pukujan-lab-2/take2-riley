"""The intake writes: one resume, email or CSV row becomes a candidate, an application and a resume version.

Everything goes through riley/store.py into one sandbox. Importing the same resume twice writes nothing the second
time; a changed resume becomes a new version on the same application, which keeps its applied date (rule 9).
"""
import re
from pathlib import Path

import yaml

from riley import store
from riley.store import _j, _j_load
from riley_web import rules

from . import extract

ROOT = Path(__file__).resolve().parents[2]


def config() -> dict:
    return yaml.safe_load((ROOT / 'screen.yaml').read_text())


def visitor_sandbox(cookie: str | None) -> str:
    """A web visitor's own sandbox: the tracker's guest id their cookie holds, or a new one, so what they import
    shows in the console too. Never 'live' (rule 16)."""
    return cookie if cookie and rules.is_guest(cookie) else rules.new_sandbox()


async def touch_sandbox(sandbox: str) -> None:
    await store.octo.query('insert into sandboxes (id) values ($1) on conflict (id) do update set last_seen = now()',
                           [sandbox])


async def jobs(sandbox: str) -> list[dict]:
    """The sandbox's jobs, drafts first, each with its criteria. `people` counts the applications Review lists, with
    the tracker's own filters (web/tracker/api.py): not merged into another person, not the bias seed's counts.
    `applicants` stays every application on the job."""
    rows = await store.octo.query(
        'select j.*, h.name as hospital, (select count(*) from applications a where a.job_id = j.id) as applicants, '
        '(select count(*) from applications a join candidates c on c.id = a.candidate_id where a.job_id = j.id '
        "and c.merged_into is null and (c.facts->>'demo') is distinct from 'bias') as people, "
        "(select coalesce(json_agg(c order by c.id), '[]') from criteria c where c.job_id = j.id) as criteria "
        'from jobs j left join hospitals h on h.id = j.hospital_id where j.sandbox = $1 '
        "order by j.status = 'draft' desc, j.created desc", [sandbox])
    out = []
    for r in rows:
        criteria = _j_load(r['criteria']) or []
        questions = [{**q, 'required': required(q, criteria)} for q in _j_load(r.get('questions')) or []]
        out.append({**r, 'criteria': criteria, 'questions': questions, 'description': _j_load(r.get('description'))})
    return out


def _words(v) -> set[str]:
    items = v if isinstance(v, list) else [v]
    return {re.sub(r'[^a-z0-9]+', ' ', str(i).lower()).strip() for i in items if i}


def required(question: dict, criteria: list[dict]) -> bool:
    """Whether a "no" to this question misses a required item: the question is a knockout, or a required criterion
    names it (its target's `question`, or its name as the id), or what a yes tells us (`fact`) is that criterion's
    target: the same license, a cert among its certs, the same shift or unit. A posted job's questions carry no `fact`,
    so there a cert or license criterion counts when the question asks about it by name."""
    if question.get('knockout') or question.get('knockout_if'):
        return True
    qid, ask = question.get('id') or '', ' ' + re.sub(r'[^a-z0-9]+', ' ', (question.get('ask') or '').lower()) + ' '
    fact: dict = question['fact'] if isinstance(question.get('fact'), dict) else {}
    for c in criteria:
        if c.get('kind') != 'required':
            continue
        target = _j_load(c.get('target')) or {}
        if qid and (target.get('question') == qid or re.sub(r'[^a-z0-9]+', '_', str(c.get('name', '')).lower())
                    .strip('_') == qid):
            return True
        for key in ('license', 'cert', 'shift', 'unit'):
            if key in target and (_words(target[key]) & _words(fact.get(key) or fact.get(f'{key}s'))):
                return True
        if not fact:
            for key in ('license', 'cert'):
                if any(w and f' {w} ' in ask for w in _words(target.get(key))):
                    return True
    return False


async def target_job(sandbox: str, job_id: int | None) -> dict:
    """The job resumes are imported for: the one picked, else the sandbox's newest open job, else the screen.yaml
    job. A closed job takes no new applications."""
    if job_id:
        rows = await store.octo.query("select * from jobs where id = $1 and sandbox = $2 and status <> 'closed'",
                                      [job_id, sandbox])
        if not rows:
            raise LookupError('no such job here')
        return rows[0]
    rows = await store.octo.query("select * from jobs where sandbox = $1 and status = 'open' order by created desc "
                                  'limit 1', [sandbox])
    return rows[0] if rows else await store.job_for(sandbox, config())


async def known_resume(candidate_id: int, job_id: int, text: str) -> bool:
    rows = await store.octo.query(
        'select 1 from resumes r join applications a on a.id = r.application_id '
        'where a.candidate_id = $1 and a.job_id = $2 and r.text = $3 limit 1', [candidate_id, job_id, text])
    return bool(rows)


async def save_prefill(application_id: int, answers: dict) -> None:
    await store.octo.query('update applications set answers = $2 where id = $1', [application_id, _j(answers)])


async def rename_placeholder(candidate: dict, name: str | None, placeholder: str) -> dict:
    """A caller first saved under the placeholder name gets their real one from the resume."""
    if not name or candidate.get('name') != placeholder:
        return candidate
    return (await store.octo.query('update candidates set name = $2 where id = $1 returning *',
                                   [candidate['id'], name]))[0]


async def import_item(sandbox: str, job: dict, item: dict) -> dict:
    """One resume into the records. status: new, reapplied (a new version), same (already on file) or skipped."""
    f = extract.resolve(item)
    out = {'label': item['label'], 'name': f.get('name'), 'facts': f}
    if not item['text']:
        return {**out, 'status': 'skipped', 'why': 'no text could be read from it'}
    if not (f.get('phone') or f.get('email') or f.get('license')):
        return {**out, 'status': 'skipped', 'why': 'no phone, email or license number, so it cannot be matched to '
                                                   'one person'}
    cfg = config()
    who = await store.upsert_candidate(sandbox, name=f.get('name'), phone=f.get('phone'), email=f.get('email'),
                                       license=f.get('license'))
    who = await rename_placeholder(who, f.get('name'), cfg['agent'].get('unknown_caller_name', 'John Doe'))
    out.update(candidate_id=who['id'], name=who.get('name'))
    if await known_resume(who['id'], job['id'], item['text']):
        return {**out, 'status': 'same'}
    app = await store.apply(sandbox, who['id'], job['id'], resume=item['text'], facts=f, source=item['source'])
    questions = _j_load(job.get('questions')) or cfg['questions']
    before = _j_load(app.get('answers')) or {}
    answers = extract.merge_prefill(before, extract.prefill(f, questions))
    if answers != before:
        await save_prefill(app['id'], answers)
    return {**out, 'status': 'reapplied' if app['reapplied'] else 'new', 'application_id': app['id'],
            'applied': app['applied'], 'changes': app['changes'], 'conflicts': app['conflicts'],
            'prefilled': sorted(k for k in answers if answers[k] != before.get(k))}


async def import_all(sandbox: str, job: dict, batch: list[dict]) -> list[dict]:
    out = []
    for item in batch:
        try:
            out.append(await import_item(sandbox, job, item))
        except Exception as e:  # one bad row never stops the batch; the page shows which one
            out.append({'label': item['label'], 'status': 'error', 'why': str(e)[:200]})
    return out
