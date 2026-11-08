"""Dana's tracker, the rules without a database: who may see and change what, masking, the bar and the order.

Guest access (docs/BUILD-PLAN.md, "Guest access for now"): every visitor gets their own sandbox id in a cookie and sees
that sandbox plus the `live` records from real calls, with phone numbers cut to the last four digits. A guest writes
only inside their own sandbox, except Dana's decision on a `live` record (and approving its draft), which the demo
needs. These functions decide that, so tests can check them for every input.
"""
import hashlib
import hmac
import re
import secrets
from datetime import datetime

LIVE = 'live'
PREFIX = 'guest-'
# A decision, the approval of its draft and calling them back with it are the only guest writes that may land on a
# live record; everything else (the outcome mode, say) changes the guest's own sandbox only
DECISIONS = frozenset({'decide', 'approve', 'call_now'})
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


def visible(row_sandbox: str, guest: str) -> bool:
    return row_sandbox in (guest, LIVE)


def can_write(row_sandbox: str, guest: str, action: str, staff: bool = False) -> bool:
    """A guest changes their own sandbox, or decides on a live record. Nothing else, ever. A signed-in member of an
    org (`staff`, whose key came from their session on the server) changes only their own org's rows."""
    if staff:
        return bool(guest) and row_sandbox == guest
    if not is_guest(guest):
        return False
    return row_sandbox == guest or (row_sandbox == LIVE and action in DECISIONS)


# ---- tenancy: who reads and writes what (docs/decisions/0022-auth-and-tenancy.md) ----

ORG_KEY = re.compile(r'org_\d+')
ROLES = ('admin', 'recruiter', 'interviewer')
# What each kind of signed-in person may open. `guest` is the demo workspace, which shows every screen on made-up data
ALLOWED = {
    'hire': frozenset({'admin', 'recruiter', 'guest'}),                  # applicants, decisions, messages, reports
    'interview': frozenset({'admin', 'recruiter', 'interviewer', 'guest'}),  # interviews and their scorecards
    'platform': frozenset({'admin', 'recruiter', 'guest'}),              # calls, traces, health, setup
    'portal': frozenset({'candidate', 'guest'}),                         # a candidate's own applications
    'invite': frozenset({'admin', 'recruiter'}),                         # never in the demo: it has no real people
}
LINK_LIMITS = {'email': (3, 15), 'ip': (10, 60)}  # at most this many sign-in links per (minutes)


def scope_of(key: str, guest: bool) -> list[str]:
    """The sandboxes a tenant reads. A guest: their own and `live` (numbers masked, as on the demo since day one).
    A signed-in org member or candidate: their org's rows only, never another's and never a guest's."""
    return [key, LIVE] if guest and key != LIVE else [key]


def readable(row_sandbox: str, scope: list[str]) -> bool:
    return row_sandbox in scope


def allowed(role: str | None, what: str) -> bool:
    return role in ALLOWED.get(what, ())


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


def admin_token(key: str | None) -> str:
    """What the admin cookie holds: a digest of TRACKER_ADMIN_KEY, never the key itself. Empty without a key."""
    return hashlib.sha256(key.encode()).hexdigest() if key else ''


def is_admin(token: str | None, key: str | None) -> bool:
    """Only a cookie or ?key= that matches the server's key; with no key set, nobody is admin."""
    expected = admin_token(key)
    return bool(expected) and bool(token) and hmac.compare_digest((token or '').encode(), expected.encode())


def can_mark_no_show(row_sandbox: str, guest: str, kinds: list[str]) -> bool:
    """"Didn't show" is offered on a card only where the guest may write it (their own sandbox, never `live`) and
    only for a booking not already marked: `kinds` is the card's `booked` and `no_show` events, oldest first."""
    return can_write(row_sandbox, guest, 'no_show') and bool(kinds) and kinds[-1] == 'booked'


def can_set_mode(target: str, guest: str, admin: bool) -> bool:
    """A guest switches the outcome mode of their own sandbox. Only Dana, holding the admin key, switches it for
    `live`, since that changes how real callers hear their outcome."""
    if target == LIVE:
        return admin
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
