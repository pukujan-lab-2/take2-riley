"""The intake API here resolves the console's sign-in exactly as pukujan-lab-2/nurse-screen does (its
docs/decisions/0022-auth-and-tenancy.md): an org member's imports land in their org's sandbox and can't reach
another org's, the demo guest keeps their own, and a request with neither cookie gets 401.

On the VPS nurse-screen-web passes /api/intake/* here with its cookies, so the session cookie is one nurse-screen
signed. riley_web/session.py is nurse-screen's web/session.py (scripts/sync_riley.py), so the signing key and the
sessions/memberships lookup are the same code. The SQL runs on a throwaway Postgres (tests/pgtest.py, synced too)
holding two orgs, so a leak shows as a row in the wrong sandbox.
"""
import asyncio
import itertools
import json
import os
from datetime import UTC, datetime, timedelta

import pgtest
import pytest
from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

from riley import octo, store
from riley_web import rules, session

try:
    import psycopg
except ImportError:  # pragma: no cover
    psycopg = None

GUEST = 'guest-0123456789ab'
SECRET = 'test-secret'
COUNTER = itertools.count()
RESUME = 'Pat Rivera\nPhone: 646-555-0123\nEmail: pat.rivera@example.com\nLicense: NY RN 7654321\n4 years on med-surg\n'
WORLD: dict = {}


@pytest.fixture(scope='module')
def pg():
    bins = pgtest.bin_dir()
    if bins is None or psycopg is None:
        if pgtest.required():
            pytest.fail('RILEY_REQUIRE_PG=1 but no Postgres (initdb) or psycopg here')
        pytest.skip('needs Postgres (initdb, pg_ctl) and psycopg')
    server = pgtest.Server(bins)
    admin = psycopg.connect(server.dsn, autocommit=True)
    admin.execute('create database t0')
    q = pgtest.runner(server.dsn.replace('dbname=postgres', 'dbname=t0'))
    os.environ['SESSION_SECRET'] = SECRET
    real = octo.query
    octo.query = q
    try:
        asyncio.run(build(q))
    finally:
        octo.query = real
        q.conn.close()  # type: ignore[attr-defined]
    yield server, admin
    admin.close()
    server.stop()


async def build(q) -> None:
    """Org A and org B, each with an open job, an admin and an interviewer, and a demo guest."""
    await store.migrate()
    for tag in ('a', 'b'):
        org = (await q('insert into orgs (key, name) values ($1, $2) returning id', [f'tmp-{tag}', f'Org {tag}']))[0]
        key = f"org_{org['id']}"
        await q('update orgs set key = $1 where id = $2', [key, org['id']])
        job = (await q("insert into jobs (sandbox, title, status) values ($1, $2, 'open') returning id",
                       [key, f'{tag.upper()}-JOB']))[0]['id']
        WORLD[tag] = {'org_id': org['id'], 'key': key, 'job': job}
        for role in ('admin', 'interviewer'):
            WORLD[f'{tag}_{role}'] = await member(q, f'{role}@{tag}.example', org['id'], role)
    await q('insert into sandboxes (id) values ($1)', [GUEST])


async def member(q, email: str, org_id: int, role: str) -> dict:
    user = (await q('insert into users (email, name) values ($1, $2) returning id', [email, email]))[0]['id']
    await q('insert into memberships (user_id, org_id, role) values ($1, $2, $3)', [user, org_id, role])
    raw = f'test-session-{next(COUNTER)}'
    await q("insert into sessions (hash, kind, user_id, org_id, expires) values ($1, 'staff', $2, $3, $4)",
            [rules.digest(raw), user, org_id, datetime.now(UTC) + timedelta(days=1)])
    # Signed as nurse-screen signs it: rules.sign with session.secret(), the same function there and here
    return {session.SESSION: rules.sign(raw, session.secret())}


@pytest.fixture
async def db(pg, monkeypatch):
    server, admin = pg
    name = f't{next(COUNTER)}'
    admin.execute(f'create database {name} template t0')
    q = pgtest.runner(server.dsn.replace('dbname=postgres', f'dbname={name}'))
    monkeypatch.setattr(octo, 'query', q)
    monkeypatch.setattr(octo, 'ready', lambda: True)
    monkeypatch.setattr(store, '_migrated', True)
    monkeypatch.setenv('SESSION_SECRET', SECRET)
    monkeypatch.delenv('TURNSTILE_SECRET', raising=False)
    session._CACHE.clear()
    yield q
    q.conn.close()  # type: ignore[attr-defined]


@pytest.fixture
async def client(db):
    from riley_web import server
    async with TestClient(TestServer(server.app())) as c:
        yield c


def cookies(who: str | None) -> dict:
    if who is None:
        return {}
    if who == 'guest':
        return {session.GUEST: GUEST}
    return dict(WORLD[who])


async def resumes(client, who: str | None, job_id=None):
    form = FormData(default_to_multipart=True)
    form.add_field('text', RESUME)
    if job_id is not None:
        form.add_field('job_id', str(job_id))
    client.session.cookie_jar.clear()
    r = await client.post('/api/intake/resumes', data=form, cookies=cookies(who))
    return r, await r.text()


async def rows_in(db, key: str) -> str:
    out = {t: await db(f'select * from {t} where sandbox = $1 order by 1', [key])
           for t in ('jobs', 'candidates', 'applications')}
    return json.dumps(out, default=str, sort_keys=True)


async def where_pat_is(db) -> list[str]:
    return [r['sandbox'] for r in await db('select sandbox from candidates where email = $1',
                                           ['pat.rivera@example.com'])]


async def test_org_as_resume_import_lands_in_org_as_sandbox(client, db):
    a, b = WORLD['a'], WORLD['b']
    before = await rows_in(db, b['key'])
    r, text = await resumes(client, 'a_admin', a['job'])
    assert r.status == 200, text
    assert json.loads(text)['results'][0]['status'] == 'new'
    assert await where_pat_is(db) == [a['key']]
    apps = await db('select a.sandbox, a.job_id from applications a join candidates c on c.id = a.candidate_id '
                    'where c.email = $1', ['pat.rivera@example.com'])
    assert apps == [{'sandbox': a['key'], 'job_id': a['job']}]
    assert session.GUEST not in r.cookies   # an org's key never goes in the demo cookie
    client.session.cookie_jar.clear()
    jobs = await client.get('/api/intake/jobs', cookies=cookies('a_admin'))
    body = await jobs.json()
    assert jobs.status == 200 and body['sandbox'] == a['key'] and [j['title'] for j in body['jobs']] == ['A-JOB']
    assert await rows_in(db, b['key']) == before


async def test_org_a_cannot_import_into_org_bs_job(client, db):
    b = WORLD['b']
    before = await rows_in(db, b['key'])
    r, text = await resumes(client, 'a_admin', b['job'])
    assert r.status == 404 and 'B-JOB' not in text
    client.session.cookie_jar.clear()
    confirm = await client.post(f"/api/intake/jobs/{b['job']}/confirm", cookies=cookies('a_admin'))
    assert confirm.status == 404
    assert await rows_in(db, b['key']) == before and await where_pat_is(db) == []


async def test_a_cookie_naming_org_bs_sandbox_is_not_org_b(client, db):
    """The demo cookie only ever holds a guest id; one set to org B's key is nobody."""
    client.session.cookie_jar.clear()
    r = await client.get('/api/intake/jobs', cookies={session.GUEST: WORLD['b']['key']})
    assert r.status == 401
    r, _ = await resumes(client, None)
    assert r.status == 401


async def test_a_forged_or_expired_session_gets_nothing(client, db):
    client.session.cookie_jar.clear()
    forged = {session.SESSION: rules.sign('test-session-0', 'another-secret')}
    assert (await client.get('/api/intake/jobs', cookies=forged)).status == 401
    await db('update sessions set expires = now() - interval \'1 minute\'')
    session._CACHE.clear()
    assert (await client.get('/api/intake/jobs', cookies=cookies('a_admin'))).status == 401


async def test_no_session_and_no_demo_cookie_is_refused_everywhere(client, db):
    client.session.cookie_jar.clear()
    for method, path in (('get', '/api/intake/jobs'), ('post', '/api/intake/job-post'),
                         ('post', f"/api/intake/jobs/{WORLD['a']['job']}/confirm")):
        r = await client.request(method, path, json={'text': 'x' * 50})
        assert r.status == 401 and 'Sign in' in await r.text(), path
    r, _ = await resumes(client, None)
    assert r.status == 401 and await where_pat_is(db) == []


async def test_an_interviewer_imports_nothing(client, db):
    r, _ = await resumes(client, 'a_interviewer', WORLD['a']['job'])
    assert r.status == 403 and await where_pat_is(db) == []


async def test_the_demo_guest_still_imports_into_their_own_sandbox(client, db):
    r, text = await resumes(client, 'guest')
    assert r.status == 200, text
    assert await where_pat_is(db) == [GUEST]
    assert r.cookies[session.GUEST].value == GUEST


def test_the_signing_key_is_nurse_screens(monkeypatch):
    """SESSION_SECRET when set, else derived from the Octo key both containers read from the same env file."""
    monkeypatch.setenv('SESSION_SECRET', 'set')
    assert session.secret() == 'set'
    monkeypatch.delenv('SESSION_SECRET')
    monkeypatch.setenv('NURSE_OCTO_KEY', 'k')
    import hashlib
    assert session.secret() == hashlib.sha256(b'riley-session:k').hexdigest()
