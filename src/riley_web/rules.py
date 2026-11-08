"""Dana's tracker, the rules without a database: who may see and change what, masking, the bar and the order.

Guest access (docs/decisions/0023-guest-lockdown.md): every demo visitor gets their own sandbox id in a cookie and reads
and writes that sandbox only, made-up people with 555-01xx numbers. They never read or change Hudson Park's real
(`live`) records, and nothing they do rings a real person. These functions decide that, so tests can check them for
every input.
"""
import hashlib
import hmac
import re
import secrets
from datetime import datetime

LIVE = 'live'
PREFIX = 'guest-'
# How outcomes run in a sandbox (track 6 owns them): the tracker only shows and switches them
MODES = {
    'immediate': ('Immediate (demo)', 'Fast decisions and call-backs right after the screen.'),
    'normal': ('Normal', 'Dana decides; everyone hears within the usual 48 hours.'),
    'dana_now': ("Dana's button", 'Dana decides, and pressing Send calls them back at once.'),
}
RESULTS = ('next_round', 'not_yet', 'not_selected')
# Five or more digits in a row (allowing spaces, dots, dashes and brackets between them) could be a phone number
_NUMBER = re.compile(r'\+?\d(?:[\s().-]*\d){4,}')


def new_sandbox() -> str:
    return PREFIX + secrets.token_hex(6)


def is_guest(sandbox: str | None) -> bool:
    """Only an id this server handed out counts; 'live', 'selftest-…' or anything typed into a cookie does not."""
    return bool(sandbox) and re.fullmatch(PREFIX + '[0-9a-f]{12}', sandbox or '') is not None


def can_write(row_sandbox: str, guest: str, action: str, staff: bool = False) -> bool:
    """Everyone changes only the rows of the one sandbox they hold: a guest their own made-up sandbox (never `live`,
    for any `action`), a signed-in member of an org (`staff`, whose key came from their session on the server) their
    own org's rows."""
    del action  # every action follows the same rule; kept so each caller still names what it does
    if staff:
        return bool(guest) and row_sandbox == guest
    return is_guest(guest) and row_sandbox == guest


def may_ring(row_sandbox: str, key: str, staff: bool, phone: str | None, fictional: bool) -> bool:
    """Whether "Call now" (or the ring inside an approval) may go ahead for this tenant: org staff on their own org's
    row, or a guest in their own sandbox with a made-up number (`fictional`, a 555-01xx one that is never dialled
    anyway). A guest never rings a real number, whatever they imported or typed."""
    if staff:
        return bool(key) and row_sandbox == key
    return is_guest(key) and row_sandbox == key and (not phone or fictional)


# ---- tenancy: who reads and writes what (docs/decisions/0022-auth-and-tenancy.md) ----

ORG_KEY = re.compile(r'org_\d+')
ROLES = ('admin', 'recruiter', 'interviewer')
PLATFORM = 'platform'   # the tenant key and role of a platform admin signed in without an org (decision 0024)
# What each kind of signed-in person may open. `guest` is the demo workspace, which shows the hiring screens on made-up
# data. `platform` (Riley's own health, keys, setup, evals and exports) is for platform admins only: PLATFORM_ADMINS,
# whatever their role in an org, never an org's staff for being staff, and never the demo
ALLOWED = {
    'hire': frozenset({'admin', 'recruiter', 'guest'}),                  # applicants, decisions, messages, reports
    'interview': frozenset({'admin', 'recruiter', 'interviewer', 'guest'}),  # interviews and their scorecards
    'calls': frozenset({'admin', 'recruiter', 'guest'}),                 # an org's own call traces; the demo's public
    'platform': frozenset({PLATFORM}),                                   # health, keys, setup, evals, exports
    'portal': frozenset({'candidate', 'guest'}),                         # a candidate's own applications
    'invite': frozenset({'admin', 'recruiter'}),                         # never in the demo: it has no real people
}
LINK_LIMITS = {'email': (3, 15), 'ip': (10, 60)}  # at most this many sign-in links per (minutes)


def scope_of(key: str, guest: bool) -> list[str]:
    """The sandboxes a tenant reads: only its own. A guest their made-up sandbox (never Hudson Park's real `live`
    rows); a signed-in org member or candidate their org's rows, never another's and never a guest's."""
    del guest  # the same for everyone since 0023; kept so callers say who they are
    return [key]


def readable(row_sandbox: str, scope: list[str]) -> bool:
    return row_sandbox in scope


def allowed(role: str | None, what: str, platform: bool = False) -> bool:
    """Whether this role opens `what`; a platform admin (`platform`) opens the platform screens too."""
    return role in ALLOWED.get(what, ()) or (platform and what == 'platform')


def may_invite(inviter: str | None, role: str) -> bool:
    """An admin invites recruiters and interviewers; a recruiter invites interviewers. Nobody invites an admin: the
    one who made the org is its admin."""
    return (inviter == 'admin' and role in ('recruiter', 'interviewer')) or (inviter == 'recruiter'
                                                                             and role == 'interviewer')


def too_many_links(by_email: int, by_ip: int) -> bool:
    """Links already asked for in the window, for this address and from this IP: one more would pass the limit."""
    return by_email >= LINK_LIMITS['email'][0] or by_ip >= LINK_LIMITS['ip'][0]


def sign(value: str, secret: str) -> str:
    """`value.mac` with an HMAC-SHA256 of the value, for a cookie the browser holds but can't make up."""
    return f'{value}.{hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()}'


def unsign(signed: str | None, secret: str) -> str | None:
    """The value of a signed cookie, or None when it is missing, malformed or signed with another secret."""
    value, _, mac = (signed or '').rpartition('.')
    if not value or not mac or not secret:
        return None
    good = hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()
    return value if hmac.compare_digest(mac.encode(), good.encode()) else None


def digest(token: str) -> str:
    """What the database keeps of a sign-in link or a session id: its SHA-256, never the thing itself."""
    return hashlib.sha256(token.encode()).hexdigest()


def can_mark_no_show(row_sandbox: str, guest: str, kinds: list[str]) -> bool:
    """"Didn't show" is offered on a card only where the guest may write it (their own sandbox, never `live`) and
    only for a booking not already marked: `kinds` is the card's `booked` and `no_show` events, oldest first."""
    return can_write(row_sandbox, guest, 'no_show') and bool(kinds) and kinds[-1] == 'booked'


def can_set_mode(target: str, guest: str) -> bool:
    """A guest switches the outcome mode of their own sandbox only. `live` is switched by its own org's staff, signed
    in, like any org (decision 0024 retired the TRACKER_ADMIN_KEY path)."""
    return is_guest(guest) and target == guest


def mask(phone: str | None) -> str:
    """A number as the tracker shows it: only its last four digits."""
    digits = re.sub(r'\D', '', phone or '')
    return f'•••• {digits[-4:]}' if digits else ''


def scrub(text: str | None) -> str:
    """Any run of digits long enough to be a phone number keeps only its last four (transcripts, summaries)."""
    return _NUMBER.sub(lambda m: mask(m.group()), text or '')


def items(questions: list[dict], answers: dict) -> list[dict]:
    """Each screen question with the answer behind it: met 'yes', 'no', 'unclear', or None when not asked yet."""
    out = []
    for q in questions or []:
        a = (answers or {}).get(q['id']) or {}
        out.append({'id': q['id'], 'ask': q.get('ask', q['id']),
                    'required': bool(q.get('knockout') or q.get('knockout_if')),
                    'answer': scrub(str(a['value'])) if 'value' in a else None, 'meets': a.get('meets'),
                    'confirmed': a.get('confirmed', True) if a else None, 'source': a.get('source'),
                    # the resume said it in other words than the job's, and the caller was asked (riley/guest.py gap)
                    'resume': scrub(str(a['resume'])) if a.get('resume') else None, 'needs': a.get('needs'),
                    'earlier': [scrub(str(e.get('answer', ''))) for e in a.get('earlier', [])]})
    return out


def bucket(status: str, score: dict | None, checks: list[dict]) -> str:
    """The bar on top of a role: 'not_read' (no screen yet), 'missing' (a required item not met or not known),
    or 'complete' (has everything)."""
    if status == 'applied' or not score:
        return 'not_read'
    if (score.get('flags') or any(c['required'] and c['meets'] != 'yes' for c in checks)):
        return 'missing'
    return 'complete'


def order(rows: list[dict]) -> list[dict]:
    """Nearest 48-hour mark first; on the same mark, complete applications first (SPEC §5)."""
    return sorted(rows, key=lambda r: (r['promised_by'] or datetime.max.isoformat(), r['bucket'] != 'complete',
                                       str(r['id'])))


def overdue(promised_by: datetime | None, status: str, now: datetime) -> bool:
    """Red on the row: the promise passed and nobody has heard yet."""
    return promised_by is not None and now > promised_by and status in ('applied', 'screened')


def summary(score: dict | None, checks: list[dict], questions_asked: list | None = None) -> list[str]:
    """The three-line call summary on the card, from the records, never from a model."""
    if not score:
        return ['Not screened yet.']
    met = [c['id'] for c in checks if c['meets'] == 'yes']
    short = [c['id'] for c in checks if c['meets'] == 'no']
    unknown = [c['id'] for c in checks if c['meets'] not in ('yes', 'no')]
    return [f"Score {score.get('score', '?')}; {score.get('next_step', 'no next step')}.",
            f"Met: {', '.join(met) or 'nothing yet'}. Not met: {', '.join(short) or 'none'}.",
            scrub(f"Still open: {', '.join(unknown) or 'nothing'}. Caller asked: "
                  f"{'; '.join(str(q) for q in questions_asked or []) or 'nothing'}.")]


def tags(kinds: set[str], other_applications: int) -> list[str]:
    """The tags on a row, from what the person did (events) and their other applications."""
    out = []
    if 'help' in kinds:
        out.append('called to check')
    if other_applications:
        out.append('applied elsewhere')
    if kinds & {'request_recruiter', 'recruiter', 'person'}:
        out.append('asked for a person')
    if 'match' in kinds:
        out.append('fits another role')
    return out


def digits_in(room: str) -> str:
    return re.sub(r'\D', '', room or '')
