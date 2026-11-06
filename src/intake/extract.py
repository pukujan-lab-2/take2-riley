"""Resume facts from files and text, without a model or a database.

The code owns the facts: name, phone, email, license, years, units, certifications and languages come from plain
patterns, so the same resume always gives the same facts, whatever format it arrived in (rule 8). Nothing pulled
from a resume counts until the person confirms it on a call (rule 5), so every prefilled answer is unconfirmed.
"""
import csv
import email
import io
import re
from email import policy
from email.message import EmailMessage
from email.utils import parseaddr

from riley.store import email_key, license_key, phone_key

FILE_KINDS = ('.pdf', '.docx', '.txt', '.md', '.eml', '.csv')

# ---- file bytes to text ----


def text_of(name: str, data: bytes) -> str:
    """The plain text of one resume file: PDF, docx or text."""
    low = name.lower()
    if low.endswith('.pdf'):
        from pypdf import PdfReader
        return '\n'.join(page.extract_text() or '' for page in PdfReader(io.BytesIO(data)).pages)
    if low.endswith('.docx'):
        import docx
        doc = docx.Document(io.BytesIO(data))
        lines = [p.text for p in doc.paragraphs]
        lines += [' | '.join(c.text for c in row.cells) for t in doc.tables for row in t.rows]
        return '\n'.join(lines)
    if low.endswith(('.txt', '.md')):
        return data.decode('utf-8-sig', errors='replace')
    raise ValueError(f'{name}: not a resume file (PDF, docx or text)')


def email_parts(data: bytes) -> tuple[str, list[tuple[str, bytes]], dict]:
    """An application email: its body, its resume attachments and who sent it."""
    msg = email.message_from_bytes(data, policy=policy.default)
    assert isinstance(msg, EmailMessage)
    body = msg.get_body(preferencelist=('plain', 'html'))
    text = body.get_content() if body else ''
    if body and body.get_content_type() == 'text/html':
        text = re.sub(r'<[^>]+>', ' ', text)
    files = []
    for part in msg.iter_attachments():
        name = part.get_filename() or ''
        payload = part.get_payload(decode=True)
        if name.lower().endswith(('.pdf', '.docx', '.txt', '.md')) and isinstance(payload, bytes):
            files.append((name, payload))
    # A forwarded email's sender is the recruiter, not the candidate
    if re.match(r'\s*(fwd?|fw)\s*:', str(msg.get('Subject', '')), re.I):
        return text, files, {}
    sender_name, sender_email = parseaddr(str(msg.get('From', '')))
    return text, files, {'name': sender_name or None, 'email': sender_email or None}


CSV_COLUMNS = {
    'name': {'name', 'fullname', 'candidatename', 'applicantname', 'candidate', 'applicant'},
    'first': {'firstname', 'first', 'givenname'},
    'last': {'lastname', 'last', 'surname', 'familyname'},
    'email': {'email', 'emailaddress', 'candidateemail', 'applicantemail'},
    'phone': {'phone', 'phonenumber', 'mobile', 'mobilephone', 'cell', 'cellphone', 'candidatephone'},
    'resume': {'resume', 'resumetext', 'resumebody', 'cv'},
}


def csv_rows(data: bytes) -> list[tuple[str, dict]]:
    """A job board's CSV export: one (text, hints) per candidate. Every column goes into the text as
    "Header: value", so a certification or years column is read like a resume line; the name, email and phone
    columns are taken as given."""
    out = []
    for row in csv.DictReader(io.StringIO(data.decode('utf-8-sig', errors='replace'))):
        cols = {re.sub(r'[^a-z]', '', (k or '').lower()): (v or '').strip() for k, v in row.items() if k}
        pick = {field: next((cols[c] for c in names if cols.get(c)), '') for field, names in CSV_COLUMNS.items()}
        name = pick['name'] or ' '.join(x for x in (pick['first'], pick['last']) if x)
        # the job they applied to says nothing about them ("RN Telemetry" is not telemetry experience)
        lines = [f'{k}: {v.strip()}' for k, v in row.items() if k and v and v.strip() and v.strip() != pick['resume']
                 and not re.search(r'job|position|requisition|title', k, re.I)]
        text = '\n'.join([*lines, pick['resume']]).strip()
        if text:
            out.append((text, {'name': name or None, 'email': pick['email'] or None, 'phone': pick['phone'] or None}))
    return out


def items(name: str, data: bytes) -> list[dict]:
    """Everything one uploaded file holds, as resumes to import: {label, text, source, hints, fallback}.
    `hints` override what the text says (a CSV's own columns); `fallback` fills only what the text lacks (an
    email's sender, who may be a recruiter forwarding it)."""
    low = name.lower()
    if low.endswith('.eml'):
        body, files, sender = email_parts(data)
        # the resume first, so its top line (the name) is read before the email's greeting and sign-off
        text = '\n\n'.join([*(text_of(n, d).strip() for n, d in files), body.strip()]).strip()
        return [{'label': name, 'text': text, 'source': 'email', 'hints': {}, 'fallback': sender}]
    if low.endswith('.csv'):
        return [{'label': f'{name} row {i}', 'text': text, 'source': 'csv', 'hints': hints, 'fallback': {}}
                for i, (text, hints) in enumerate(csv_rows(data), 1)]
    return [{'label': name, 'text': text_of(name, data), 'source': 'upload', 'hints': {}, 'fallback': {}}]


def pasted(text: str) -> dict:
    return {'label': 'pasted text', 'text': text.strip(), 'source': 'paste', 'hints': {}, 'fallback': {}}


# ---- text to facts ----

PHONE = re.compile(r'(?<![\d+])(?:\+?1[\s.\-]*)?\(?(\d{3})\)?[\s.\-]*(\d{3})[\s.\-]*(\d{4})(?!\d)')
EMAIL = re.compile(r'[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}')
STATES = {
    'new york': 'NY', 'new jersey': 'NJ', 'connecticut': 'CT', 'pennsylvania': 'PA', 'massachusetts': 'MA',
    'florida': 'FL', 'california': 'CA', 'texas': 'TX', 'illinois': 'IL', 'ohio': 'OH', 'georgia': 'GA',
    'maryland': 'MD', 'virginia': 'VA', 'north carolina': 'NC', 'michigan': 'MI', 'vermont': 'VT',
    'rhode island': 'RI', 'delaware': 'DE', 'arizona': 'AZ', 'colorado': 'CO', 'washington': 'WA',
}
CODES = set(STATES.values()) | {
    'AL', 'AK', 'AR', 'HI', 'ID', 'IN', 'IA', 'KS', 'KY', 'LA', 'ME', 'MN', 'MS', 'MO', 'MT', 'NE',
    'NV', 'NH', 'NM', 'ND', 'OK', 'OR', 'SC', 'SD', 'TN', 'UT', 'WV', 'WI', 'WY', 'DC', 'PR'}
LICENSE_TYPES = ('RN', 'LPN', 'LVN', 'CNA', 'NP', 'APRN')
# Unit names as nurses write them. Abbreviations match only in capitals ("ER", not "her").
UNITS = {
    'med-surg': r'(?i:med(?:ical)?[\s/\-]*surg(?:ical|ery)?)',
    'telemetry': r'(?i:\btelemetry\b)',
    'step-down': r'(?i:step[\s\-]*down)|\bPCU\b',
    'icu': r'\b[MSC]?ICU\b|\bCCU\b|(?i:intensive care|critical care)',
    'emergency': r'\bED\b|\bER\b|(?i:emergency (?:department|room|nursing|medicine))',
    'labor and delivery': r'(?i:labor (?:and|&) delivery)|\bL&D\b',
    'nicu': r'\bNICU\b|(?i:neonatal intensive)',
    'pediatrics': r'(?i:pediatric|\bpeds\b)',
    'rehab': r'(?i:\brehab(?:ilitation)?\b)',
    'long-term care': r'(?i:long[\s\-]term care|nursing home|skilled nursing)|\bLTC\b|\bSNF\b',
    'oncology': r'(?i:oncology)',
    'telehealth': r'(?i:telehealth|tele[\s\-]?triage|telephone triage|virtual care)',
}
CERTS = {
    'BLS': r'\bBLS\b|(?i:basic life support)', 'ACLS': r'\bACLS\b|(?i:advanced cardiac life support)',
    'PALS': r'\bPALS\b|(?i:pediatric advanced life support)', 'NRP': r'\bNRP\b|(?i:neonatal resuscitation)',
    'TNCC': r'\bTNCC\b', 'CCRN': r'\bCCRN\b', 'CEN': r'\bCEN\b', 'IV': r'(?i:\bIV (?:therapy )?certifi)',
    'EKG': r'\bEKG\b|\bECG\b', 'phlebotomy': r'(?i:phlebotomy)', 'CNA': r'\bCNA\b', 'PCT': r'\bPCT\b',
}
LANGUAGES = ('English', 'Spanish', 'Mandarin', 'Cantonese', 'Chinese', 'Haitian Creole', 'French', 'Russian',
             'Bengali', 'Arabic', 'Korean', 'Tagalog', 'Polish', 'Yiddish', 'Italian', 'Portuguese', 'Hindi', 'Urdu')
WORD_NUMBERS = {w: i for i, w in enumerate([
    'zero', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine',
    'ten', 'eleven', 'twelve', 'thirteen', 'fourteen', 'fifteen'])}
YEARS = re.compile(r'\b(\d{1,2}(?:\.\d)?|' + '|'.join(WORD_NUMBERS) + r')\s*\+?\s*(?:years?|yrs?)\b', re.I)
NOT_A_NAME = {'resume', 'cv', 'curriculum', 'vitae', 'nurse', 'registered', 'rn', 'lpn', 'cna', 'license',
              'experience', 'summary', 'objective', 'profile', 'contact', 'phone', 'email', 'education', 'skills',
              'certifications', 'languages', 'from', 'to', 'subject', 'dear', 'hello', 'hi', 'application'}


def phones(text: str) -> list[str]:
    """Every US phone number in the text, as the store's key (digits only, no country code)."""
    return [a + b + c for a, b, c in PHONE.findall(text)]


def _name(text: str) -> str | None:
    labelled = re.search(r'^\s*(?:full\s+)?name\s*[:\-]\s*(.+)$', text, re.I | re.M)
    lines = [labelled.group(1)] if labelled else text.splitlines()[:8]
    for line in lines:
        line = re.sub(r',.*$', '', line.strip().strip('|')).strip()   # "Ana Morales, RN, BSN"
        words = line.split()
        if (2 <= len(words) <= 4 and re.fullmatch(r"[A-Za-z][A-Za-z.'\- ]+", line)
                and not {w.lower().strip('.,') for w in words} & NOT_A_NAME):
            return ' '.join(w.capitalize() if w.isupper() or w.islower() else w for w in words)
    return None


def _license(text: str) -> dict:
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if not re.search(r'licen[sc]e|\blic\b', line, re.I):
            continue
        # a "License" heading with the number on the line under it
        span = line if re.search(r'\d{5}', line) else ' '.join(lines[i:i + 2])
        number = re.search(r'\b([A-Z]{0,2}\d{5,8})\b', span)
        if not number:
            continue
        low = span.lower()
        state = next((code for full, code in STATES.items() if full in low), None) or next(
            (w for w in re.findall(r'\b[A-Z]{2}\b', span) if w in CODES), None)
        kind = next((t for t in LICENSE_TYPES if re.search(rf'\b{t}\b', span)), None)
        out = {'license_number': number.group(1), 'license': f'{state}-{number.group(1)}' if state
               else number.group(1)}
        if state:
            out['license_state'] = state
        if kind:
            out['license_type'] = kind
        return out
    return {}


def _years(text: str) -> int | float | None:
    found = []
    for n in YEARS.findall(text):
        v = WORD_NUMBERS.get(n.lower()) if n.lower() in WORD_NUMBERS else float(n)
        if v is not None and 0 < v <= 50:
            found.append(v)
    if not found:
        return None
    top = max(found)
    return int(top) if top == int(top) else top


def facts(text: str) -> dict:
    """What a resume says, in the shape `resumes.facts` keeps. Only keys that were found are present."""
    out: dict = {}
    if name := _name(text):
        out['name'] = name
    if found := phones(text):
        out['phone'] = found[0]
    if m := EMAIL.search(text):
        out['email'] = email_key(m.group(0))
    out.update(_license(text))
    if (years := _years(text)) is not None:
        out['years'] = years
    if units := sorted(u for u, p in UNITS.items() if re.search(p, text)):
        out['units'] = units
    if certs := sorted(c for c, p in CERTS.items() if re.search(p, text)):
        out['certifications'] = certs
    # a "Languages" line, or the line under a "Languages" heading
    spoken = ' '.join(re.findall(r'^.*(?:language|bilingual|fluent|speak).*(?:\n.*)?$', text, re.I | re.M))
    if langs := [lang for lang in LANGUAGES if re.search(rf'\b{lang}\b', spoken, re.I)]:
        out['languages'] = langs
    return out


def resolve(item: dict) -> dict:
    """The facts for one import: the text's, with the item's own hints over them and its fallback under them."""
    def keyed(d: dict) -> dict:
        d = {k: v for k, v in d.items() if v}
        if 'phone' in d:
            d['phone'] = phone_key(d['phone'])
        if 'email' in d:
            d['email'] = email_key(d['email'])
        return {k: v for k, v in d.items() if v}
    out = {**keyed(item.get('fallback') or {}), **facts(item['text']), **keyed(item.get('hints') or {})}
    if 'license' in out:
        out['license'] = license_key(out['license'])
    return out


# ---- facts to prefilled answers ----

ACUTE = {'med-surg', 'telemetry', 'step-down', 'icu', 'emergency'}


def _at_least(good: str | None) -> float | None:
    m = re.search(r'at least\s+(\d+(?:\.\d+)?)', good or '')
    return float(m.group(1)) if m else None


def prefill(f: dict, questions: list[dict]) -> dict:
    """Answers the resume gives to this job's questions, in the stored shape. Always source 'resume' and never
    confirmed: Riley reads them back and they count only once the person says they are right (rule 5)."""
    ids = {q['id']: q for q in questions}
    out = {}

    def put(qid: str, value: str, meets: str) -> None:
        if qid in ids:
            out[qid] = {'value': value, 'meets': meets, 'source': 'resume', 'confirmed': False}
    if 'license' in f:
        kind = f.get('license_type', 'nursing')
        where = f.get('license_state', 'state not given')
        put('license', f"{kind} license {f['license_number']}, {where}", 'yes' if where == 'NY' else 'unclear')
    if 'years' in f:
        need = _at_least(ids.get('years_rn', {}).get('good'))
        put('years_rn', f"{f['years']:g} years", 'unclear' if need is None else 'yes' if f['years'] >= need else 'no')
    if f.get('units'):
        put('acute_care', ', '.join(f['units']), 'yes' if ACUTE & set(f['units']) else 'unclear')
    if f.get('certifications'):
        put('certifications', ', '.join(f['certifications']), 'yes' if 'BLS' in f['certifications'] else 'unclear')
    return out


def merge_prefill(existing: dict, new: dict) -> dict:
    """A new resume replaces only earlier unconfirmed resume answers. Anything the person confirmed, or said on a
    call, stays as it is."""
    out = dict(existing or {})
    for qid, v in new.items():
        old = out.get(qid)
        if old is None or (old.get('source') == 'resume' and not old.get('confirmed')):
            out[qid] = v
    return out
