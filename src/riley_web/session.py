"""Who a request is, from its cookies alone (docs/decisions/0022-auth-and-tenancy.md): a signed-in member or
candidate of one org (`ns_session`), the demo guest (`ns_guest`), or nobody. Never from a request's body or query.

This is the part of web/auth.py that other services need too: pukujan-lab-2/take2-riley serves /api/intake/* behind
this site (web/server.py intake_proxy) and resolves the same cookie the same way. Its scripts/sync_riley.py copies this
file to src/riley_web/session.py, so keep it to agent/ and web/tracker/rules.py imports (both are synced as well).
"""
import hashlib
import json
import os
import secrets
import time
from dataclasses import dataclass, field

from aiohttp import web

from riley import octo, store
from riley_web import rules

SESSION = 'ns_session'
GUEST = 'ns_guest'          # web/tracker/api.py COOKIE
CACHE_SECONDS = 30          # a session is read from the database at most this often per process
_DEV_SECRET = secrets.token_hex(32)
_CACHE: dict[str, tuple[float, 'Tenant | None']] = {}


def secret() -> str:
    """The key that signs session cookies: SESSION_SECRET, else one derived from the Octo key (so the VPS needs no new
    setting and a restart keeps everyone signed in), else a random one for this process (development)."""
    if value := os.environ.get('SESSION_SECRET'):
        return value
    if key := os.environ.get('NURSE_OCTO_KEY'):
        return hashlib.sha256(f'riley-session:{key}'.encode()).hexdigest()
    return _DEV_SECRET


@dataclass(frozen=True)
class Tenant:
    kind: str                       # staff | candidate | guest
    key: str                        # the sandbox this tenant's rows carry, and the only one it writes
    role: str                       # admin | recruiter | interviewer | candidate | guest
    org: dict | None = None         # {id, name}
    user: dict | None = None        # staff: {id, email, name}
    account: dict | None = None     # candidate: {id, email}
    extra: dict = field(default_factory=dict)

    @property
    def guest(self) -> bool:
        return self.kind == 'guest'

    @property
    def staff(self) -> bool:
        return self.kind == 'staff'

    @property
    def scope(self) -> list[str]:
        return rules.scope_of(self.key, self.guest)

    @property
    def who(self) -> str:
        """The name a decision or an approval is written under."""
        if self.user:
            return self.user.get('name') or self.user['email']
        return f'Dana ({self.key})'


def _error(status: int, message: str) -> web.HTTPException:
    cls = {401: web.HTTPUnauthorized, 403: web.HTTPForbidden, 400: web.HTTPBadRequest, 429: web.HTTPTooManyRequests}
    return cls[status](text=json.dumps({'error': message}), content_type='application/json')


async def session_of(request: web.Request) -> Tenant | None:
    raw = rules.unsign(request.cookies.get(SESSION), secret())
    if not raw:
        return None
    h = rules.digest(raw)
    hit = _CACHE.get(h)
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    rows = await octo.query(
        'select s.kind, s.org_id, o.key, o.name as org_name, u.id as user_id, u.email, u.name, m.role, '
        'ca.id as account_id, ca.email as account_email from sessions s join orgs o on o.id = s.org_id '
        'left join users u on u.id = s.user_id '
        'left join memberships m on m.user_id = s.user_id and m.org_id = s.org_id '
        'left join candidate_accounts ca on ca.id = s.candidate_account_id and ca.org_id = s.org_id '
        'where s.hash = $1 and s.revoked is null and s.expires > now()', [h])
    tenant = None
    if rows:
        r = rows[0]
        org = {'id': int(r['org_id']), 'name': r['org_name']}
        if r['kind'] == 'staff' and r['role'] in rules.ROLES and r['user_id'] is not None:
            tenant = Tenant('staff', r['key'], r['role'], org,
                            user={'id': int(r['user_id']), 'email': r['email'], 'name': r['name']})
        elif r['kind'] == 'candidate' and r['account_id'] is not None:
            tenant = Tenant('candidate', r['key'], 'candidate', org,
                            account={'id': int(r['account_id']), 'email': r['account_email']})
    _CACHE[h] = (time.monotonic(), tenant)
    return tenant


async def tenant_of(request: web.Request) -> Tenant | None:
    """The signed-in person, else the demo guest whose cookie this server handed out, else nobody."""
    if 'tenant' in request:
        return request['tenant']
    tenant = None
    if octo.ready() and request.cookies.get(SESSION):
        await store.ensure()
        tenant = await session_of(request)
    if tenant is None and rules.is_guest(request.cookies.get(GUEST)):
        tenant = Tenant('guest', request.cookies[GUEST], 'guest')
    request['tenant'] = tenant
    return tenant


async def need(request: web.Request, what: str) -> Tenant:
    """The tenant, when they may open `what` (rules.ALLOWED); else 401 for nobody and 403 for the wrong role."""
    tenant = await tenant_of(request)
    if tenant is None:
        raise _error(401, 'Sign in first, or try the demo workspace.')
    if not rules.allowed(tenant.role, what):
        raise _error(403, 'Your role here does not open this.')
    return tenant
