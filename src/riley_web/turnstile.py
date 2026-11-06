"""Cloudflare Turnstile, the one bot check for every web form that takes applications (track 13).

It runs only when TURNSTILE_SECRET is set; with no keys every form works as before, and the per-visitor limits and
the daily cap still hold. With the secret, a submission passes only on a clear success from siteverify: no token, a
network error or an odd reply fails, and the refusal sends the person to the phone line instead, so nobody is shut
out. The page shows the widget when TURNSTILE_SITE_KEY is set.
"""
import os

import httpx
from aiohttp import web

SITEVERIFY = 'https://challenges.cloudflare.com/turnstile/v0/siteverify'
# The POST routes that take an application; web/server.py NO_CHECK says why each other POST route needs none
FORMS = ('/api/call-me', '/api/intake/job-post', '/api/intake/resumes')
HEADER = 'X-Turnstile'        # for a multipart form, where the token can't sit in a JSON body
TRANSPORT: httpx.AsyncBaseTransport | None = None   # tests put a stand-in for Cloudflare here


def site_key() -> str | None:
    return os.environ.get('TURNSTILE_SITE_KEY') or None


async def passed(token: str, ip: str, transport: httpx.AsyncBaseTransport | None = None) -> bool:
    secret = os.environ.get('TURNSTILE_SECRET')
    if not secret:
        return True
    if not token:
        return False
    data = {'secret': secret, 'response': token, **({'remoteip': ip} if ip and ip != '?' else {})}
    try:
        async with httpx.AsyncClient(timeout=5, transport=transport or TRANSPORT) as c:
            reply = (await c.post(SITEVERIFY, data=data)).json()
    except (httpx.HTTPError, ValueError):
        return False  # can't tell: treat as failed; the refusal still gives them the phone line
    return isinstance(reply, dict) and reply.get('success') is True


def refusal(call_in: str | None) -> dict:
    where = f'call Riley at {call_in} instead' if call_in else 'call Riley instead'
    return {'error': f'The check did not pass. Please {where}.', 'call_in': call_in}


def ip_of(request: web.Request) -> str:
    return request.headers.get('CF-Connecting-IP') or request.remote or '?'


async def guard(request: web.Request, call_in: str | None, body: dict | None = None) -> web.Response | None:
    """None when the form may go on; else the 403 to send. Runs before the form reads or writes anything else."""
    token = str((body or {}).get('turnstile') or request.headers.get(HEADER) or '')
    if await passed(token, ip_of(request)):
        return None
    return web.json_response(refusal(call_in), status=403)
