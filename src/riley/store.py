"""The hiring records: jobs, people, applications and everything that happens to them, in the nurse-screen Octo \
workspace.

Built on riley/octo.py. The worker, the help line, the scheduler and the web app all read and write through here.
The rules that decide dates, matches and undo windows are plain functions, so tests can check them without a database.

Every row carries `sandbox`: 'live' for real candidates, or a visitor's own id on the demo site, so no visitor
ever sees another's records (SPEC rule 16). Demographics and accommodations sit in their own tables and nothing
that scores or shows a candidate to a recruiter joins them (rules 11 and 12).
"""
import asyncio
import json
import os
import re
from datetime import UTC, datetime, timedelta

from . import octo

REPLY_HOURS = 48     # everyone hears within 48 hours of applying (SPEC 4)
WINDOW_HOURS = 48    # a job holds decisions this long after its first applicant
UNDO_DAYS = 7        # a withdrawal can be undone this long

SCHEMA = """
create table if not exists jobs (
  id bigserial primary key,
  sandbox text not null default 'live',
  title text not null,
  location text,
  facts jsonb not null default '{}',        -- pay, shifts, unit, start: what Riley may say
  questions jsonb not null default '[]',    -- the screen, shaped like screen.yaml
  bar int not null default 70,              -- the score that books round 2
  window_hours int not null default 48,
  first_applied timestamptz,                -- starts the review window
  referral_points boolean not null default false,
  late_referral_bonus boolean not null default false,
  slots jsonb not null default '[]',        -- Dana's open interview times
  status text not null default 'draft',     -- draft until the recruiter confirms, then open or closed
  created timestamptz not null default now()
);
create table if not exists criteria (
  id bigserial primary key,
  job_id bigint not null references jobs(id),
  kind text not null check (kind in ('required', 'weighted', 'bonus')),
  name text not null,
  points int not null default 0,
  target jsonb,                             -- e.g. {"years": 2}, for partial credit
  mappings jsonb not null default '[]'      -- transferable skills, only once the recruiter approves them
);
create table if not exists candidates (
  id bigserial primary key,
  sandbox text not null default 'live',
  name text,
  phone text,                               -- digits only, so one person never gets two profiles
  email text,
  license text,                             -- state and number, e.g. NY-123456
  language text not null default 'en',
  consent jsonb not null default '{}',      -- calls, texts, keep_details, other_roles
  timezone text not null default 'America/New_York',
  merged_into bigint references candidates(id),
  created timestamptz not null default now()
);
create table if not exists applications (
  id bigserial primary key,
  sandbox text not null default 'live',
  candidate_id bigint not null references candidates(id),
  job_id bigint not null references jobs(id),
  applied timestamptz not null default now(),   -- never moves on reapply: they keep their place
  updated timestamptz not null default now(),
  promised_by timestamptz not null,
  status text not null default 'applied',
  answers jsonb not null default '{}',      -- {id: {value, source, confirmed}}
  score jsonb,
  decision jsonb,                           -- {result, reason, by, at}
  rank int,                                 -- the candidate's own preference
  referral jsonb,                           -- {name, unit, confirmed}
  withdrawn timestamptz,
  unique (candidate_id, job_id)
);
create table if not exists resumes (
  id bigserial primary key,
  candidate_id bigint not null references candidates(id),
  application_id bigint references applications(id),
  source text not null,                     -- upload, email, paste, csv, phone
  text text not null,
  facts jsonb not null default '{}',
  at timestamptz not null default now()
);
create table if not exists events (
  id bigserial primary key,
  sandbox text not null default 'live',
  candidate_id bigint references candidates(id),
  application_id bigint references applications(id),
  kind text not null,                       -- call, help, text, status, date_moved, note, merge, ...
  detail jsonb not null default '{}',
  due timestamptz,                          -- set for scheduled work; the scheduler picks these up
  done timestamptz,
  at timestamptz not null default now()
);
create table if not exists feedback (
  id bigserial primary key,
  candidate_id bigint not null references candidates(id),
  application_id bigint references applications(id),
  words text not null,
  share_with_recruiter boolean not null default false,
  at timestamptz not null default now()
);
create table if not exists accommodations (
  id bigserial primary key,
  candidate_id bigint not null references candidates(id),
  way text not null,                        -- text, human, more_time, relay. Never why.
  at timestamptz not null default now()
);
create table if not exists self_id (
  candidate_id bigint primary key references candidates(id),
  answers jsonb not null,                   -- optional EEO answers, read only by the bias check
  at timestamptz not null default now()
);
create table if not exists sandboxes (
  id text primary key,
  created timestamptz not null default now(),
  last_seen timestamptz not null default now()
);
create index if not exists candidates_phone on candidates (sandbox, phone);
create index if not exists candidates_email on candidates (sandbox, email);
create index if not exists candidates_license on candidates (sandbox, license);
create index if not exists applications_job on applications (job_id, status);
create index if not exists events_due on events (due) where done is null;

-- Several hospitals of one employer, their jobs' shifts and units, and what a person has confirmed about themselves
-- (docs/BUILD-PLAN.md, "Data model"). Added with alter so a workspace made before them keeps its rows.
create table if not exists hospitals (
  id bigserial primary key,
  sandbox text not null default 'live',
  name text not null,
  borough text,
  address text,
  timezone text not null default 'America/New_York'
);
alter table jobs add column if not exists hospital_id bigint references hospitals(id);
alter table jobs add column if not exists unit text;
alter table jobs add column if not exists shift text;           -- days, nights, evenings, weekends, per diem
alter table jobs add column if not exists hours text;           -- e.g. 3 x 12 h
alter table jobs add column if not exists pay text;
alter table jobs add column if not exists options jsonb not null default '[]';  -- what Dana allows: another shift, ...
alter table criteria add column if not exists reason text;      -- Dana's words when it falls short
alter table criteria add column if not exists change text;      -- what would change it
alter table criteria add column if not exists send_without_asking boolean not null default false;
alter table candidates add column if not exists facts jsonb not null default '{}';  -- confirmed, about the person
-- Outcome messages: Riley's draft from Dana's decision, her edits and approval, and delivery (track 4).
-- applications.decision is {result: next_round | not_yet | not_selected, reason, criteria: [ids], by, at}.
create table if not exists messages (
  id bigserial primary key,
  sandbox text not null default 'live',
  application_id bigint not null references applications(id),
  kind text not null,                       -- next_round, not_yet, not_selected
  status text not null default 'draft',     -- draft, approved, sent, delivered, dropped
  parts jsonb not null default '{}',        -- result, reason, change, offers, options, next_step: each from records
  text text not null,
  edits jsonb not null default '[]',        -- [{by, at, text}]
  approved_by text,
  approved timestamptz,
  delivered timestamptz,
  channel text,                             -- call, text, email
  created timestamptz not null default now()
);
create index if not exists jobs_hospital on jobs (sandbox, hospital_id, status);
create index if not exists messages_app on messages (application_id, status);
-- The bias check (track 12): a job where one group moves forward far less often switches to "Dana confirms every
-- no" (riley/bias.py). self_id itself is in the first block above and is read only by the bias check.
alter table jobs add column if not exists confirm_every_no boolean not null default false;

-- Track 6 outcomes: one setting per sandbox, e.g. outcome_mode = immediate | normal | dana_now (riley/outcome.py).
create table if not exists settings (
  id bigserial primary key,
  sandbox text not null default 'live',
  key text not null,
  value jsonb not null,
  updated timestamptz not null default now(),
  unique (sandbox, key)
);
-- Track 9, "call me" for guests: what a guest typed on /try ({first_name, phone, email, token}) and when the purge
-- wipes it and the phone (riley/guest.py). The made-up name, the application and the calls stay.
alter table candidates add column if not exists guest jsonb;
alter table candidates add column if not exists expires timestamptz;
create index if not exists candidates_expires on candidates (expires) where guest is not null;
-- The recruiter console (web/console.py): the file a vacancy was opened from, and job-board postings. Postings are a
-- demo record only: nothing is sent to any board until each one grants partner access.
alter table jobs add column if not exists source_file text;
-- The posting as people read it on the console: {about, duties, requirements, preferred}. Riley's call uses facts only.
alter table jobs add column if not exists description jsonb;
create table if not exists job_postings (
  id bigserial primary key,
  sandbox text not null,
  job_id bigint not null references jobs(id) on delete cascade,
  board text not null,
  status text not null default 'posted',      -- posted | removed
  posted_at timestamptz not null default now(),
  removed_at timestamptz
);
create index if not exists job_postings_job on job_postings (job_id, board);

-- Sign-in and tenancy (docs/decisions/0022-auth-and-tenancy.md). An employer is an org; its `key` is the `sandbox`
-- value every one of its rows carries. Hudson Park Health, whose rows were always 'live', is the org with key 'live';
-- a new org gets 'org_<id>'; the demo stays a visitor's own 'guest-…' sandbox with no org. Only added tables and
-- columns, so the rows already in the workspace keep working as they are.
create table if not exists orgs (
  id bigserial primary key,
  key text not null unique,                 -- the sandbox its rows carry
  name text not null,
  created timestamptz not null default now()
);
insert into orgs (key, name) values ('live', 'Hudson Park Health') on conflict (key) do nothing;
create table if not exists users (
  id bigserial primary key,
  email text not null unique,               -- lower-cased; the only way in is a link to it
  name text,
  created timestamptz not null default now(),
  last_seen timestamptz
);
create table if not exists memberships (
  id bigserial primary key,
  user_id bigint not null references users(id),
  org_id bigint not null references orgs(id),
  role text not null check (role in ('admin', 'recruiter', 'interviewer')),
  invited_by bigint references users(id),
  created timestamptz not null default now(),
  unique (user_id, org_id)
);
-- A candidate who signed in to one employer's page by a link to the email on their applications. Their
-- applications are always found again by that email inside that org; candidate_ids is what matched last time.
create table if not exists candidate_accounts (
  id bigserial primary key,
  org_id bigint not null references orgs(id),
  email text not null,
  candidate_ids jsonb not null default '[]',
  created timestamptz not null default now(),
  last_seen timestamptz,
  unique (org_id, email)
);
-- Sign-in links. Only a SHA-256 of the token is kept; the token itself is only in the email. Single use, 15 minutes.
create table if not exists login_tokens (
  id bigserial primary key,
  hash text not null unique,
  email text not null,
  purpose text not null check (purpose in ('signup', 'login', 'invite', 'candidate')),
  org_id bigint references orgs(id),
  detail jsonb not null default '{}',       -- signup {name, org}; invite {name, role, invited_by}
  ip text,
  expires timestamptz not null,
  used timestamptz,
  created timestamptz not null default now()
);
create index if not exists login_tokens_email on login_tokens (email, created);
create index if not exists login_tokens_ip on login_tokens (ip, created);
-- A signed-in browser. The cookie holds a random id and its HMAC; only a SHA-256 of the id is kept here, so a copy
-- of this table signs nobody in. Signing out sets revoked.
create table if not exists sessions (
  hash text primary key,
  kind text not null check (kind in ('staff', 'candidate')),
  user_id bigint references users(id),
  candidate_account_id bigint references candidate_accounts(id),
  org_id bigint not null references orgs(id),
  created timestamptz not null default now(),
  expires timestamptz not null,
  revoked timestamptz
);
-- A platform admin (decision 0024) may sign in without being in any org: kind 'platform', no org
-- sessions may hold a platform admin in no org (decision 0024): migrate() changes it once, outside this script
-- Who opens Riley's own engineering screens (health, keys, setup, evals, exports): PLATFORM_ADMINS, copied here on
-- start so take2-riley's intake, which reads the same sessions, agrees without its own setting
create table if not exists platform_admins (
  email text primary key,
  added timestamptz not null default now()
);
-- Riley's written summary of one screen for Dana (riley/summary.py), under the application's own sandbox
create table if not exists summaries (
  application_id bigint primary key,
  sandbox text not null,                    -- the application's own tenant key (decision 0022)
  fingerprint text not null,                -- of the record it was written from (summary.fingerprint)
  summary jsonb,                            -- {decision, lines, row}; null when writing failed
  how jsonb not null default '{}',          -- model, tokens, seconds, what the checks dropped, error
  at timestamptz not null default now()
);
create index if not exists summaries_sandbox on summaries (sandbox);
-- Per-org Riley (riley/tenant.py): the numbers that ring an org. A number no org owns is Hudson Park's (`live`).
-- orgs.employer and orgs.signature (how candidates hear and read the org's name) are added by org_columns() below,
-- once, because an ALTER on orgs would lock the table every sign-in reads
create table if not exists org_numbers (
  number text primary key,                  -- ten digits (store.phone_key)
  org_id bigint not null references orgs(id),
  added timestamptz not null default now()
);
"""
_migrated = False


# ---- rules, without a database ----

def phone_key(phone: str | None) -> str | None:
    """Digits only, with a US country code dropped, so +1 (212) 555-0100 and 2125550100 match."""
    if not phone:
        return None
    digits = re.sub(r'\D', '', phone)
    return digits[1:] if len(digits) == 11 and digits.startswith('1') else digits or None


def fictional(phone: str | None) -> bool:
    """A 555-01xx number: reserved for fiction in North America, so it is never dialled (the guest cast's numbers)."""
    key = phone_key(phone) or ''
    return len(key) == 10 and key[3:6] == '555' and key[6:8] == '01'


def email_key(email: str | None) -> str | None:
    return email.strip().lower() or None if email else None


def license_key(license: str | None) -> str | None:
    return re.sub(r'[^A-Z0-9-]', '', license.upper()) or None if license else None


def promised_by(applied: datetime) -> datetime:
    return applied + timedelta(hours=REPLY_HOURS)


def window_closes(job: dict) -> datetime | None:
    first = job.get('first_applied')
    return first + timedelta(hours=job.get('window_hours', WINDOW_HOURS)) if first else None


def can_decide(job: dict, now: datetime) -> bool:
    """No decision lands before the job's review window closes (rule 3)."""
    closes = window_closes(job)
    return closes is not None and now >= closes


def can_undo(withdrawn: datetime | None, now: datetime) -> bool:
    return withdrawn is not None and now - withdrawn <= timedelta(days=UNDO_DAYS)


def resume_changes(old: dict, new: dict) -> tuple[list[str], list[str]]:
    """What changed between two resume versions, and which facts now disagree.

    Additions are just changes. A fact both versions state differently is a conflict: Riley asks them to confirm
    that one item, and it never lowers a score (rule 8).
    """
    changes, conflicts = [], []
    for k in sorted(set(old) | set(new)):
        a, b = old.get(k), new.get(k)
        if a == b:
            continue
        if a is None:
            changes.append(f'added {k}: {b}')
        elif b is None:
            changes.append(f'dropped {k}')
        else:
            changes.append(f'{k} {a} → {b}')
            conflicts.append(k)
    return changes, conflicts


# ---- reads and writes ----

def ts(v) -> datetime | None:
    """Octo returns timestamps as ISO text."""
    return datetime.fromisoformat(v.replace('Z', '+00:00')) if isinstance(v, str) else v


def _j(v):
    return json.dumps(v) if isinstance(v, (dict, list)) else v


async def migrate() -> None:
    await octo.migrate()  # calls and eval_runs first: the web app reads calls.sandbox before any call is saved
    await octo.query(SCHEMA)
    await platform_sessions()
    await org_columns()


async def platform_sessions() -> None:
    """Let sessions hold a platform admin in no org, once. An ALTER takes the table's strongest lock, so it runs only
    while still needed: running it on every start deadlocked the web app's own startup tasks."""
    rows = await octo.query("select pg_get_constraintdef(oid) as d from pg_constraint "
                            "where conname = 'sessions_kind_check'", [])
    if not rows or 'platform' not in (rows[0]['d'] or ''):
        await octo.query('alter table sessions drop constraint if exists sessions_kind_check', [])
        await octo.query("alter table sessions add constraint sessions_kind_check "
                         "check (kind in ('staff', 'candidate', 'platform'))", [])
    rows = await octo.query("select is_nullable from information_schema.columns where table_name = 'sessions' "
                            "and column_name = 'org_id'", [])
    if rows and rows[0]['is_nullable'] == 'NO':
        await octo.query('alter table sessions alter column org_id drop not null', [])


# Columns added to orgs after it shipped: the employer name candidates hear and read (else orgs.name) and the recruiter
# signature on outcome emails (else the name of the person who decided)
ORG_COLUMNS = {'employer': 'text', 'signature': 'text'}


async def org_columns() -> None:
    """Add the orgs columns that are still missing, and only those: the catalog is read first, so a start with them in
    place takes no lock on orgs (the lesson of the sessions migration, decision 0024)."""
    rows = await octo.query("select column_name from information_schema.columns where table_name = 'orgs'", [])
    have = {r['column_name'] for r in rows}
    for name, kind in ORG_COLUMNS.items():
        if name not in have:
            await octo.query(f'alter table orgs add column if not exists {name} {kind}', [])


async def ensure() -> None:
    """Create or extend the tables once per process; every statement is if-not-exists, so it is safe to repeat. The
    startup tasks (scheduler, purge, tracker) all call it at once, so one runs the migration and the rest wait."""
    global _migrated
    if _migrated:
        return
    async with _lock():
        if not _migrated:
            await migrate()
            await platform_admins()
            _migrated = True


_LOCKS: dict = {}


def _lock() -> asyncio.Lock:
    """One lock per event loop (tests run several loops in one process)."""
    loop = asyncio.get_running_loop()
    return _LOCKS.setdefault(id(loop), asyncio.Lock())


async def platform_admins() -> None:
    """Make the platform_admins table match PLATFORM_ADMINS (comma-separated emails), when it is set. A process
    without the setting (take2-riley's intake) leaves the table as the web app wrote it."""
    listed = os.environ.get('PLATFORM_ADMINS')
    if listed is None:
        return
    emails = sorted({e for e in (email_key(x) for x in listed.split(',')) if e})
    await octo.query('delete from platform_admins where not (email = any($1))', [emails])
    for email in emails:
        await octo.query('insert into platform_admins (email) values ($1) on conflict (email) do nothing', [email])


async def find_candidate(sandbox: str, phone=None, email=None, license=None) -> dict | None:
    """The one profile for this person, matched by license, then phone, then email."""
    for col, key in (('license', license_key(license)), ('phone', phone_key(phone)), ('email', email_key(email))):
        if key:
            rows = await octo.query(
                f'select * from candidates where sandbox = $1 and {col} = $2 and merged_into is null limit 1',
                [sandbox, key])
            if rows:
                return rows[0]
    return None


async def upsert_candidate(sandbox: str, name=None, phone=None, email=None, license=None, language=None) -> dict:
    """Find the person or add them. Details they didn't have before are filled in; known ones are kept."""
    found = await find_candidate(sandbox, phone, email, license)
    keys = {'name': name, 'phone': phone_key(phone), 'email': email_key(email), 'license': license_key(license),
            'language': language}
    if found:
        fill = {k: v for k, v in keys.items() if v and not found.get(k)}
        if not fill:
            return found
        sets = ', '.join(f'{k} = ${i}' for i, k in enumerate(fill, 2))
        rows = await octo.query(f'update candidates set {sets} where id = $1 returning *',
                                [found['id'], *fill.values()])
        return rows[0]
    cols = ['sandbox'] + [k for k, v in keys.items() if v]
    vals = [sandbox] + [v for v in keys.values() if v]
    marks = ', '.join(f'${i}' for i in range(1, len(cols) + 1))
    rows = await octo.query(f'insert into candidates ({", ".join(cols)}) values ({marks}) returning *', vals)
    return rows[0]


async def apply(sandbox: str, candidate_id: int, job_id: int, resume: str | None = None, facts: dict | None = None,
                source: str = 'upload', now: datetime | None = None) -> dict:
    """Apply, or apply again. Reapplying updates the same application, keeps the original date and promise, and
    saves the resume as a new version. Returns the application plus `reapplied`, `changes` and `conflicts`."""
    now = now or datetime.now(UTC)
    existing = await octo.query('select * from applications where candidate_id = $1 and job_id = $2',
                                [candidate_id, job_id])
    if existing:
        app = (await octo.query('update applications set updated = $2 where id = $1 returning *',
                                [existing[0]['id'], now]))[0]
    else:
        app = (await octo.query(
            'insert into applications (sandbox, candidate_id, job_id, applied, updated, promised_by) '
            'values ($1, $2, $3, $4, $4, $5) returning *', [sandbox, candidate_id, job_id, now, promised_by(now)]))[0]
        # only an application in the job's own sandbox counts (a /try guest applies to a Hudson Park job from `try`)
        await octo.query('update jobs set first_applied = coalesce(first_applied, $2) where id = $1 and sandbox = $3',
                         [job_id, now, sandbox])
    changes, conflicts = [], []
    if resume:
        last = await octo.query('select facts from resumes where candidate_id = $1 order by at desc limit 1',
                                [candidate_id])
        if last:
            changes, conflicts = resume_changes(last[0]['facts'] or {}, facts or {})
        await octo.query('insert into resumes (candidate_id, application_id, source, text, facts, at) '
                         'values ($1, $2, $3, $4, $5, $6)',
                         [candidate_id, app['id'], source, resume, _j(facts or {}), now])
    await log(sandbox, 'reapplied' if existing else 'applied', candidate_id, app['id'],
              {'changes': changes, 'conflicts': conflicts, 'source': source})
    return {**app, 'reapplied': bool(existing), 'changes': changes, 'conflicts': conflicts}


async def log(sandbox: str, kind: str, candidate_id=None, application_id=None, detail=None, due=None) -> None:
    await octo.query('insert into events (sandbox, candidate_id, application_id, kind, detail, due) '
                     'values ($1, $2, $3, $4, $5, $6)',
                     [sandbox, candidate_id, application_id, kind, _j(detail or {}), due])


async def withdraw(application_id: int, now: datetime | None = None) -> None:
    await octo.query("update applications set withdrawn = $2, status = 'withdrawn' where id = $1",
                     [application_id, now or datetime.now(UTC)])


async def undo_withdraw(application_id: int, now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    rows = await octo.query('select withdrawn from applications where id = $1', [application_id])
    if not rows or not can_undo(ts(rows[0]['withdrawn']), now):
        return False
    # Back where it was: a screened application stays screened
    await octo.query("update applications set withdrawn = null, "
                     "status = case when score is not null then 'screened' else 'applied' end where id = $1",
                     [application_id])
    return True


async def restart_screen(application_id: int) -> None:
    """Back to the start: the answers, score and decision are cleared and the application is applied again, so the
    next call screens it afresh. The old answers and decision stay in `events`."""
    await octo.query("update applications set answers = '{}'::jsonb, score = null, decision = null, "
                     "status = 'applied', updated = now() where id = $1", [application_id])


async def restart_after_outcome(sandbox: str, phone: str) -> int | None:
    """The demo's fourth call: once a caller has heard their outcome (a delivered message, none still waiting), their
    own next call starts the interview again. Returns the application restarted, or None."""
    rows = await octo.query(
        'select a.id, a.candidate_id from applications a join candidates c on c.id = a.candidate_id '
        "where c.sandbox = $1 and c.phone = $2 and c.merged_into is null and a.withdrawn is null "
        "and a.status in ('interview', 'not_yet', 'not_selected') "
        "and exists (select 1 from messages m where m.application_id = a.id and m.status = 'delivered') "
        "and not exists (select 1 from messages m where m.application_id = a.id and m.status = 'approved') "
        'order by a.applied desc limit 1', [sandbox, phone_key(phone)])
    if not rows:
        return None
    await restart_screen(int(rows[0]['id']))
    await log(sandbox, 'retake', rows[0]['candidate_id'], rows[0]['id'], {'by': 'their call after the outcome'})
    return int(rows[0]['id'])


async def reopen(application_id: int) -> None:
    """A fact behind a no was corrected: the application goes back to Dana to read (screened), with the decision
    cleared. The old decision stays in `events`."""
    await octo.query("update applications set status = 'screened', decision = null, updated = now() "
                     "where id = $1 and status in ('not_yet', 'not_selected')", [application_id])


async def status(sandbox: str, phone: str) -> list[dict]:
    """What the help line reads out: every application this caller has, with its date, status and promise.
    Only their own; nothing about other candidates beyond each job's count (rule 13)."""
    rows = await octo.query(
        'select a.id, a.candidate_id, c.name, c.timezone, j.title, j.location, a.applied, a.updated, a.promised_by, '
        'a.status, a.withdrawn, '
        '(select count(*) from applications o where o.job_id = a.job_id) as applicants '
        'from applications a join jobs j on j.id = a.job_id join candidates c on c.id = a.candidate_id '
        'where c.sandbox = $1 and c.phone = $2 and c.merged_into is null order by a.applied',
        [sandbox, phone_key(phone)])
    # A no carries its why: the last message about it, each item with what they said on the call
    for r in rows:
        if r['status'] in ('not_yet', 'not_selected'):
            m = await octo.query("select parts from messages where application_id = $1 and status in "
                                 "('approved', 'delivered') order by id desc limit 1", [r['id']])
            r['why'] = why(_j_load(m[0]['parts']) if m else {})
    return rows


def why(parts: dict) -> str:
    """A no's reasons in one line: each item's reason, with what they said on the call and where the resume differed."""
    bits = []
    for i in (parts or {}).get('items') or []:
        said = (f"resume showed {i['name']}, but on the call they said {i['heard']}" if i.get('resume_differs')
                else f"on the call they said {i['heard']}" if i.get('heard') else '')
        bits.append(' '.join(x for x in (i.get('reason'), f'({said})' if said else '') if x))
    return '; '.join(b for b in [(parts or {}).get('reason', ''), *bits] if b)


def call_answers(apps: list[dict], current_id: int) -> tuple[dict, dict]:
    """Split what is already known for a call about application `current_id` into
    (from the resume, waiting to be confirmed) and (confirmed on an earlier call for another role).
    Answers are stored as {id: {value, source, confirmed, meets}}; the screener uses {answer, meets, source}."""
    def as_screen(v: dict) -> dict:
        out = {'answer': v['value'], 'meets': v.get('meets', 'unclear'), 'source': v.get('source', 'resume')}
        # a resume answer in other words than the job's (riley/guest.py gap): what the job needs, for the one question
        return {**out, 'needs': v['needs']} if v.get('needs') else out
    resume, earlier = {}, {}
    for a in apps:
        for qid, v in (_j_load(a.get('answers')) or {}).items():
            if a['id'] == current_id and not v.get('confirmed'):
                resume[qid] = as_screen(v)
            elif a['id'] != current_id and v.get('confirmed'):
                earlier.setdefault(qid, as_screen(v))
    return resume, earlier


def stored_answers(screen_answers: dict, prefilled: dict | None = None) -> dict:
    """The call's answers in the stored shape. Where the resume said it in other words than the job's (an item with
    `needs`) and the caller answered on the call, the record keeps all three for Dana: `resume` (what it said),
    `needs` (what the job asks for) and the value (what they said)."""
    out = {}
    for i, v in screen_answers.items():
        out[i] = {'value': v['answer'], 'meets': v['meets'], 'source': v.get('source', 'call'),
                  'confirmed': v.get('confirmed', True), **({'earlier': v['earlier']} if v.get('earlier') else {})}
        if (was := (prefilled or {}).get(i)) and was.get('needs') and out[i]['source'] == 'call':
            out[i] |= {'resume': was['answer'], 'needs': was['needs']}
    return out


def _j_load(v):
    return json.loads(v) if isinstance(v, str) else v


async def for_call(sandbox: str, phone: str, application_id=None) -> tuple[dict | None, dict, dict]:
    """The application a call from this number is about (the one a first screen call Riley placed names, else their
    newest one not yet screened), with what the resume says and what they already confirmed for another role.
    (None, {}, {}) for an unknown number."""
    apps = await octo.query(
        'select a.id, a.candidate_id, a.job_id, a.status, a.answers, a.applied, c.name, j.title, j.questions, '
        'j.facts as job_facts, j.unit, j.shift, j.hours, h.name as hospital from applications a '
        'join candidates c on c.id = a.candidate_id join jobs j on j.id = a.job_id '
        'left join hospitals h on h.id = j.hospital_id '
        'where c.sandbox = $1 and c.phone = $2 and c.merged_into is null and a.withdrawn is null '
        'order by a.applied desc', [sandbox, phone_key(phone)])
    named = [a for a in apps if application_id is not None and str(a['id']) == str(application_id)]
    current = next((a for a in named + apps if a['status'] == 'applied'), None)
    if not current:
        return None, {}, {}
    return (current, *call_answers(apps, current['id']))


async def save_answers(application_id: int, screen_answers: dict, score: dict, prefilled: dict | None = None) -> None:
    await octo.query("update applications set answers = answers || $2, score = $3, status = 'screened', "
                     "updated = now() where id = $1",
                     [application_id, _j(stored_answers(screen_answers, prefilled)), _j(score)])


async def job_for(sandbox: str, cfg: dict) -> dict:
    """The open job screen.yaml describes, added once (a phone caller with no resume applies to this one). Once
    jobs/seed.yaml is seeded it is the seeded Midtown med-surg job (`agent.job`), never a second one."""
    title = cfg['agent'].get('title') or cfg['agent']['role']
    rows = await octo.query("select * from jobs where sandbox = $1 and title in ($2, $3) and status = 'open' "
                            'order by hospital_id nulls last limit 1',
                            [sandbox, title, cfg['agent'].get('job') or title])
    if rows:
        return rows[0]
    location = str(cfg['job_facts'].get('location', '')).split(',')[0] or None
    return (await octo.query("insert into jobs (sandbox, title, location, facts, questions, status) "
                             "values ($1, $2, $3, $4, $5, 'open') returning *",
                             [sandbox, title, location, _j(cfg['job_facts']), _j(cfg['questions'])]))[0]


async def save_new_caller(sandbox: str, phone: str, cfg: dict, screen_answers: dict, score: dict,
                          job_id: int | None = None) -> dict:
    """A screen from a number not on file: the person (with the placeholder name until a real one comes), their
    application to the job they chose on the call (else screen.yaml's) and its answers, so a call back from that
    number finds them."""
    who = await upsert_candidate(sandbox, name=cfg['agent'].get('unknown_caller_name', 'John Doe'), phone=phone)
    app = await apply(sandbox, who['id'], job_id or (await job_for(sandbox, cfg))['id'], source='phone')
    await save_answers(app['id'], screen_answers, score)
    return app


# ---- the bias check (track 12): EEO answers are written and read only below and in riley/bias.py ----

async def save_self_id(candidate_id: int, answers: dict) -> None:
    """Add or change a person's optional EEO answers. Nothing else is written, not even an event (rule 12)."""
    from .bias import clean  # only the listed answers are ever stored
    await octo.query('insert into self_id (candidate_id, answers, at) values ($1, $2, now()) '
                     'on conflict (candidate_id) do update set answers = excluded.answers, at = now()',
                     [candidate_id, _j(clean(answers))])


async def drop_self_id(candidate_id: int) -> None:
    await octo.query('delete from self_id where candidate_id = $1', [candidate_id])



async def bias_rows(sandbox: str) -> list[dict]:
    """One row per decided application with self_id answers: the job, the answers and whether it moved forward.
    No name, id or number, so nothing downstream can tie the answers to a person."""
    return await octo.query(
        "select a.job_id, s.answers, (a.decision->>'result') = 'next_round' as selected from applications a "
        'join self_id s on s.candidate_id = a.candidate_id where a.sandbox = $1 and a.decision is not null',
        [sandbox], row_limit=5000)


async def has_self_id(sandbox: str) -> bool:
    return bool(await octo.query('select 1 from self_id s join candidates c on c.id = s.candidate_id '
                                 'where c.sandbox = $1 limit 1', [sandbox]))


async def bias_jobs(sandbox: str) -> list[dict]:
    return await octo.query(
        'select j.id, j.title, h.name as hospital, j.referral_points, j.confirm_every_no from jobs j '
        "left join hospitals h on h.id = j.hospital_id where j.sandbox = $1 and j.status <> 'closed' order by j.id",
        [sandbox])


async def demo_candidate(sandbox: str) -> int | None:
    """One made-up person from the bias seed, so a visitor can open the optional form as a candidate would."""
    rows = await octo.query("select id from candidates where sandbox = $1 and facts->>'demo' = 'bias' "
                            'order by id limit 1', [sandbox])
    return rows[0]['id'] if rows else None


async def touch_sandbox(sandbox: str) -> None:
    await octo.query('insert into sandboxes (id) values ($1) on conflict (id) do update set last_seen = now()',
                     [sandbox])


async def set_confirm_every_no(job_id: int) -> None:
    await octo.query('update jobs set confirm_every_no = true where id = $1', [job_id])


async def seed_bias_demo(sandbox: str, people: list[dict]) -> None:
    """Made-up people for a visitor's sandbox (riley/bias.py demo_people), in one statement: each a candidate, a
    decided application and self_id answers. Marked demo in their facts."""
    await octo.query(
        'with p as (select * from jsonb_to_recordset($2::jsonb) '
        'as x(key text, name text, job_id bigint, result text, answers jsonb)), '
        "c as (insert into candidates (sandbox, name, email, facts) select $1, name, key, '{\"demo\": \"bias\"}' "
        'from p returning id, email), '
        "a as (insert into applications (sandbox, candidate_id, job_id, promised_by, status, decision) "
        "select $1, c.id, p.job_id, now() + interval '48 hours', 'decided', "
        "jsonb_build_object('result', p.result, 'reason', 'made-up demo data', 'by', 'demo seed', 'at', now()) "
        'from c join p on p.key = c.email returning candidate_id) '
        'insert into self_id (candidate_id, answers) select c.id, p.answers from c join p on p.key = c.email',
        [sandbox, _j(people)])


# ---- the guest cast (riley/seed.py cast): made-up candidates for a visitor's tracker ----

async def has_demo_cast(sandbox: str, demo: str) -> bool:
    return bool(await octo.query("select 1 from candidates where sandbox = $1 and facts->>'demo' = $2 limit 1",
                                 [sandbox, demo]))


async def seed_demo_cast(sandbox: str, demo: str, people: list[dict]) -> int:
    """The cast in one statement: each person, their application, Riley's message when Dana decided, and their
    events (transcript, decision, booking). Nothing is written when the sandbox already has a cast, so running it
    twice, or twice at once, writes it once. Returns how many people were written."""
    rows = await octo.query(
        'with p as (select * from jsonb_to_recordset($2::jsonb) as x(key text, name text, phone text, language text, '
        'consent jsonb, facts jsonb, job_id bigint, applied timestamptz, promised_by timestamptz, status text, '
        'answers jsonb, score jsonb, decision jsonb, message jsonb, events jsonb) '
        "where not exists (select 1 from candidates where sandbox = $1 and facts->>'demo' = $3)), "
        'c as (insert into candidates (sandbox, name, phone, email, language, consent, facts) '
        'select $1, name, phone, key, language, consent, facts from p returning id, email), '
        'a as (insert into applications (sandbox, candidate_id, job_id, applied, updated, promised_by, status, '
        'answers, score, decision) select $1, c.id, p.job_id, p.applied, p.applied, p.promised_by, p.status, '
        'p.answers, p.score, p.decision from c join p on p.key = c.email returning id, candidate_id), '
        'pa as (select p.*, a.id as app_id, a.candidate_id from a join c on c.id = a.candidate_id '
        'join p on p.key = c.email), '
        'm as (insert into messages (sandbox, application_id, kind, status, parts, text, channel, approved_by, '
        "approved, delivered) select $1, app_id, message->>'kind', message->>'status', message->'parts', "
        "message->>'text', $4, message->>'approved_by', "
        "case when message->>'status' in ('approved', 'delivered') then now() end, "
        "case when message->>'status' = 'delivered' then now() end from pa where message is not null "
        'returning id, application_id), '
        'j as (update jobs set first_applied = coalesce(first_applied, (select min(applied) from p)) '
        'where id in (select job_id from p) returning id), '
        'e as (insert into events (sandbox, candidate_id, application_id, kind, detail) '
        "select $1, pa.candidate_id, pa.app_id, ev->>'kind', (ev->'detail') || "
        "case when m.id is not null and ev->>'kind' in ('booked', 'decision', 'delivered') "
        "then jsonb_build_object('message_id', m.id) else '{}'::jsonb end "
        'from pa cross join jsonb_array_elements(pa.events) ev left join m on m.application_id = pa.app_id '
        'returning id) '
        'select count(*) as n from c',
        [sandbox, _j(people), demo, 'call; email shown and downloadable (no email provider yet)'])
    return int(rows[0]['n']) if rows else 0

# ---- what a person confirmed about themselves (track 5, matching) ----

async def save_facts(candidate_id: int, facts: dict) -> dict:
    """Merge a screen's facts into `candidates.facts` (keys this screen answered replace the old ones) and their
    answer about other roles into `consent`. Returns the merged facts."""
    facts = dict(facts)
    consent = {'other_roles': facts.pop('other_roles')} if 'other_roles' in facts else {}
    rows = await octo.query('update candidates set facts = facts || $2, consent = consent || $3 where id = $1 '
                            'returning facts', [candidate_id, _j(facts), _j(consent)])
    return _j_load(rows[0]['facts']) if rows else facts


SANDBOX_DAYS = 7   # a demo workspace (`guest-…`) nobody opened for this long is deleted, every row of it


async def purge_sandboxes(now: datetime | None = None, days: int = SANDBOX_DAYS) -> list[str]:
    """Delete every row of the demo sandboxes unseen for `days` (web/session.py marks them seen; web/server.py
    purge_hourly runs it), children first.
    Only `guest-` sandboxes: never `live`, `try` or an org. Returns the sandboxes removed."""
    cutoff = (now or datetime.now(UTC)) - timedelta(days=days)
    rows = await octo.query("select id from sandboxes where id ~ '^guest-[0-9a-f]{12}$' and last_seen < $1",
                            [cutoff])
    gone = [r['id'] for r in rows]
    if not gone:
        return []
    people = 'select id from candidates where sandbox = any($1)'
    apps = 'select id from applications where sandbox = any($1)'
    for sql in (f'delete from resumes where candidate_id in ({people}) or application_id in ({apps})',
                f'delete from feedback where candidate_id in ({people}) or application_id in ({apps})',
                f'delete from accommodations where candidate_id in ({people})',
                f'delete from self_id where candidate_id in ({people})',
                'delete from events where sandbox = any($1)',
                'delete from messages where sandbox = any($1)',
                'delete from summaries where sandbox = any($1)',
                'delete from calls where sandbox = any($1)',
                'delete from settings where sandbox = any($1)',
                'delete from job_postings where sandbox = any($1)',
                'delete from applications where sandbox = any($1)',
                'delete from criteria where job_id in (select id from jobs where sandbox = any($1))',
                'update candidates set merged_into = null where sandbox = any($1)',
                'delete from candidates c where c.sandbox = any($1) and not exists '
                '(select 1 from candidates o where o.merged_into = c.id and not (o.sandbox = any($1)))',
                'delete from jobs where sandbox = any($1) and not exists '
                '(select 1 from applications a where a.job_id = jobs.id)',
                'delete from hospitals h where h.sandbox = any($1) and not exists '
                '(select 1 from jobs j where j.hospital_id = h.id)',
                'delete from sandboxes where id = any($1)'):
        await octo.query(sql, [gone])
    return gone
