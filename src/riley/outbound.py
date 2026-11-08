"""Riley calls out: a new room with the nurse-screen agent dispatched into it, then LiveKit dials the person in.

Only when SIP_OUTBOUND_TRUNK_ID is set (the owner's Telnyx trunk `riley-telnyx`, mapped from LIVEKIT_OUTBOUND_TRUNK
in riley/keys.py). Without it nothing is dialled and `place` says why, so the web page offers Riley's number instead.

The room and the dispatch carry only ids and the mode ({'mode': 'guest' | 'outcome' | 'status' | 'callback' |
'follow_up' | 'screen', 'candidate_id', 'application_id', 'job_id'?, 'message_id'?, 'event_ids'?}); riley/main.py reads
them, and the application, job or candidate named decides which org the call is for (riley/tenant.py). 'screen' is the
first screen call an org's staff ask for, for people they imported (web/console.py api_screen).
The scheduler (riley/scheduler.py) places 'callback' (finish the screen they asked to be called back for) and
'follow_up' (reminders, a missed interview, a moved date: the events named in `event_ids`). The phone number goes only
to the SIP request, never into a room name or metadata, so it never reaches the calls table or the results page. A
555-01xx number (made up, like the guest cast's) is never dialled.

Every call names the sandbox it is for (`may_dial`). A demo sandbox (a console guest's own, or /try's `try`) rings only
the /try visitor's own number, the one they typed and ticked consent for, never a number a guest imported in a resume
or typed into a message: a demo visitor must never cause a call to a real person.
"""
import json
import os
import re
import secrets

from .store import _j_load, fictional, phone_key

TRY = 'try'  # riley/guest.py SANDBOX: the /try visitors, apart from Hudson Park's real `live` records
GUEST = re.compile(r'guest-[0-9a-f]{12}')  # web/tracker/rules.py is_guest: a console demo visitor's sandbox

AGENT = 'nurse-screen'
MODES = ('guest', 'outcome', 'status', 'callback', 'follow_up', 'screen')
# SIP answers that mean the person didn't pick up: timeout, unavailable, busy, cancelled, declined
NO_ANSWER = {408, 480, 486, 487, 603}


def room_metadata(meta: dict) -> str:
    """The room's metadata: the mode and record ids, nothing else (no phone, no name)."""
    if meta.get('mode') not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    keep = {k: meta[k] for k in ('mode', 'candidate_id', 'application_id', 'job_id', 'message_id')
            if meta.get(k) is not None}
    if meta.get('event_ids'):
        keep['event_ids'] = [int(i) for i in meta['event_ids']]
    return json.dumps(keep)


def demo(sandbox: str | None) -> bool:
    return sandbox == TRY or GUEST.fullmatch(sandbox or '') is not None


def consented(person: dict, phone: str | None) -> bool:
    """Whether `phone` is the /try visitor's own number, typed on /try with the consent box ticked: a `try` candidate
    whose `guest` record holds that number. `person` is a candidate or an application row with `sandbox` and
    `guest`. A guest's imported resume has no `guest` record, so it never counts."""
    typed = _j_load(person.get('guest')) or {}
    return (person.get('sandbox') == TRY and isinstance(typed, dict) and bool(phone_key(phone))
            and phone_key(typed.get('phone')) == phone_key(phone))


def may_dial(sandbox: str | None, phone: str | None, consent: bool = False) -> str | None:
    """Why this call may not be placed, or None. A call that names no sandbox is never placed; a demo sandbox only
    rings the visitor's own consented number (a made-up 555-01xx one is never dialled anyway)."""
    if not sandbox:
        return 'no sandbox named: never dialled'
    if fictional(phone):
        return 'a made-up 555-01xx number: never dialled'
    if demo(sandbox) and not consent:
        return "a demo rings only the visitor's own number, asked for on /try"
    return None


def answered(error: Exception) -> bool | None:
    """False when the dial failed because nobody picked up (a SIP status in NO_ANSWER); None for any other failure,
    which says nothing about the person."""
    code = getattr(error, 'sip_status_code', None)
    if code is None:
        raw = (getattr(error, 'metadata', None) or {}).get('sip_status_code')
        code = int(raw) if isinstance(raw, str) and raw.isdigit() else raw
    return False if code in NO_ANSWER else None


async def place(phone: str, metadata: dict, lk=None, wait: bool = False, *, sandbox: str | None = None,
                consented: bool = False) -> dict:
    """Call `phone` (E.164) with Riley for a record in `sandbox`. {'placed': bool, 'why': str}. `consented`: the
    number is the /try visitor's own (`consented()`); only then does a demo sandbox ring. `lk` is a LiveKitAPI, for
    tests. With `wait` (the scheduler) it returns once they pick up or don't, and says which in 'answered'."""
    if why := may_dial(sandbox, phone, consented):  # before the trunk: the refusal is the same everywhere
        return {'placed': False, 'why': why}
    trunk = os.environ.get('SIP_OUTBOUND_TRUNK_ID')
    if not trunk:
        return {'placed': False, 'why': 'no outbound trunk'}
    from livekit import api
    meta = room_metadata(metadata)
    # The store keeps ten digits; the trunk dials E.164
    digits = ''.join(c for c in phone if c.isdigit())
    phone = f'+1{digits}' if len(digits) == 10 else f'+{digits}'
    room = f'out-{secrets.token_hex(6)}'  # out- rooms are phone calls Riley placed
    own = lk is None
    lk = lk or api.LiveKitAPI()
    try:
        await lk.room.create_room(api.CreateRoomRequest(name=room, metadata=meta, empty_timeout=120))
        await lk.agent_dispatch.create_dispatch(api.CreateAgentDispatchRequest(agent_name=AGENT, room=room,
                                                                               metadata=meta))
        caller_id = os.environ.get('RILEY_CALLER_ID')
        await lk.sip.create_sip_participant(api.CreateSIPParticipantRequest(
            sip_trunk_id=trunk, sip_call_to=phone, sip_number=caller_id or '', room_name=room,
            participant_identity='callee', participant_name='Callee', play_dialtone=False,
            wait_until_answered=wait))
        return {'placed': True, 'why': room, **({'answered': True} if wait else {})}
    except Exception as e:  # a failed dial leaves the record waiting, like no trunk
        print('outbound call failed:', type(e).__name__, e, flush=True)
        out = {'placed': False, 'why': f'dial failed: {type(e).__name__}'}
        return {**out, 'answered': answered(e)} if wait else out
    finally:
        if own:
            await lk.aclose()
