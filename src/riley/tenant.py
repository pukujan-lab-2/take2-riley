"""Which employer a call is for, and how Riley names that employer (docs/decisions/0025-per-org-riley.md).

Every org's rows carry its key in `sandbox` (decision 0022). A call Riley places names the application, job or
candidate it is about in its room metadata, so that row's sandbox is the call's. A call that comes in is for the org
that owns the number dialled (`org_numbers`); a number no org owns is Hudson Park's (`live`), as it always was.

Hudson Park and the demo keep screen.yaml's names (Hudson Park, Dana). A real org (`org_<id>`) is named by its row:
`orgs.employer`, else its name, for the employer; for the recruiter, the first line of `orgs.signature` up to a comma,
else "our recruiter" on a call. A message names the person who decided (riley/outcome.py decide).
"""
import re

from . import octo, store

LIVE = 'live'
ORG = re.compile(r'org_\d+')
TABLES = (('application_id', 'applications'), ('job_id', 'jobs'), ('candidate_id', 'candidates'))
TEAM = 'our recruiter'   # a real org's recruiter on a call, until its signature names one


def is_org(sandbox: str | None) -> bool:
    return bool(sandbox) and ORG.fullmatch(sandbox or '') is not None


async def _sandbox_of(table: str, row_id) -> str | None:
    try:
        rid = int(row_id)
    except (TypeError, ValueError):
        return None
    rows = await octo.query(f'select sandbox from {table} where id = $1', [rid])
    return rows[0]['sandbox'] if rows else None


async def for_number(dialled: str | None) -> str:
    """The org that owns the number this call came in on; Hudson Park's for any number no org owns."""
    key = store.phone_key(dialled)
    if not key:
        return LIVE
    rows = await octo.query('select o.key from org_numbers n join orgs o on o.id = n.org_id where n.number = $1',
                            [key])
    return rows[0]['key'] if rows else LIVE


async def home(meta: dict, dialled: str | None = None) -> str:
    """The sandbox a call is for: the record its metadata names (a call Riley placed, a browser call for a job), else
    the org of the number dialled."""
    for field, table in TABLES:
        if meta.get(field) is not None and (found := await _sandbox_of(table, meta[field])):
            return found
    return await for_number(dialled)


async def org(sandbox: str) -> dict | None:
    """A real org's row (id, key, name, employer, signature), or None for Hudson Park, the demo and /try."""
    if not is_org(sandbox):
        return None
    rows = await octo.query('select id, key, name, employer, signature from orgs where key = $1', [sandbox])
    return rows[0] if rows else None


def spoken_recruiter(signature: str | None) -> str:
    """The recruiter as Riley names them on a call: the signature's first line up to a comma ("Ada Byron, Talent
    team" is Ada Byron), else "our recruiter"."""
    first = (signature or '').strip().splitlines()[0].split(',')[0].strip() if (signature or '').strip() else ''
    return first[:60] or TEAM


def names(row: dict | None, cfg: dict) -> dict:
    """{employer, recruiter, signature}: how this org is named. Hudson Park and the demo: screen.yaml's."""
    dana = cfg.get('dana', {})
    if not row:
        return {'employer': cfg['agent']['employer'], 'mail_employer': dana.get('employer', cfg['agent']['employer']),
                'recruiter': cfg['agent']['recruiter'], 'signature': None, 'org': False}
    employer = (row.get('employer') or '').strip() or row['name']
    return {'employer': employer, 'mail_employer': employer, 'recruiter': spoken_recruiter(row.get('signature')),
            'signature': (row.get('signature') or '').strip() or None, 'org': True}


async def names_for(sandbox: str, cfg: dict) -> dict:
    return names(await org(sandbox), cfg)


def branded(cfg: dict, said: dict) -> dict:
    """screen.yaml with this org's names in place of Hudson Park's and Dana's, for every line Riley says on the call.
    Dana's recorded clips are Hudson Park's, so a real org's outcome call goes without them."""
    if not said.get('org'):
        return cfg
    return {**cfg, 'agent': {**cfg['agent'], 'employer': said['employer'], 'recruiter': said['recruiter']},
            'dana': {**cfg.get('dana', {}), 'employer': said['mail_employer'], 'clips': False}}
