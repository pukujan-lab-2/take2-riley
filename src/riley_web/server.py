"""The candidate intake page, moved here from pukujan-lab-2/nurse-screen web/server.py with the parsers (src/intake/).

A recruiter (or a judge) drops resumes on /intake: PDF, docx, text, an application .eml or a job board's CSV, or
pastes them. src/intake/extract.py reads each into facts and prefilled answers, with no model and no database, and
src/intake/load.py writes the candidates and applications into the visitor's own sandbox in the nurse-screen Octo
workspace, never `live`. When that candidate calls, Riley confirms what the resume says instead of asking it again
(riley/main.py, store.for_call). A pasted job post is drafted by Claude (src/intake/jobpost.py) and stays a draft
until confirmed. Sample files to try it with are in samples/.

riley.design-bakery.com is served by nurse-screen-web, which passes /intake and /api/intake/* here (INTAKE_URL), so
the page, its cookie and the nav to the other pages stay on one site.

Run:  uv run python -m riley_web.server      (from src/; HOST and PORT, default 127.0.0.1:8091)
"""

import json
import os
import time
from pathlib import Path

import yaml
from aiohttp import BodyPartReader, web

from intake import extract, jobpost, load
from riley import keys, store
from riley_web import turnstile

keys.load()
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SANDBOX_COOKIE = 'ns_guest'  # the tracker's guest cookie (nurse-screen web/tracker/api.py COOKIE): one sandbox for both
DRAFTS: dict[str, list[float]] = {}
MAX_FILES = 50


def call_in() -> str | None:
    """Riley's number, for anyone a form can't take: the bot check's refusal."""
    return yaml.safe_load((ROOT / 'screen.yaml').read_text()).get('guests', {}).get('call_in')


async def intake_page(_):
    return web.FileResponse(HERE / 'intake.html')


async def intake_sandbox(request) -> str:
    sandbox = load.visitor_sandbox(request.cookies.get(SANDBOX_COOKIE))
    await store.ensure()
    await load.touch_sandbox(sandbox)
    return sandbox


def intake_reply(sandbox: str, body, status: int = 200) -> web.Response:
    resp = web.json_response(body, status=status, dumps=lambda o: json.dumps(o, default=str))
    resp.set_cookie(SANDBOX_COOKIE, sandbox, max_age=30 * 86400, httponly=True, samesite='Lax')
    return resp


async def api_intake_jobs(request):
    sandbox = await intake_sandbox(request)
    return intake_reply(sandbox, {'sandbox': sandbox, 'jobs': await load.jobs(sandbox),
                                  'site_key': turnstile.site_key()})


async def job_post_body(request) -> tuple[dict, str | None]:
    """JSON {text}, or multipart with one `file` (PDF, docx or text) or `text`: the post's text and the file's name."""
    if not request.content_type.startswith('multipart/'):
        try:
            body = await request.json()
        except ValueError:
            body = {}
        return (body if isinstance(body, dict) else {}), None
    text, name = '', None
    reader = await request.multipart()
    while (part := await reader.next()) is not None:
        if not isinstance(part, BodyPartReader):
            continue
        if part.name == 'file' and part.filename:
            name = part.filename
            text = extract.text_of(name, await part.read())
        elif part.name == 'text' and not text:
            text = await part.text()
    return {'text': text}, name


async def api_intake_job_post(request):
    # A draft costs a model call, so the bot check first, then a few an hour per visitor. A multipart upload carries
    # the bot check's token in a header, so the file is read only after it passes
    multipart = request.content_type.startswith('multipart/')
    if multipart and (blocked := await turnstile.guard(request, call_in())):
        return blocked
    try:
        body, source_file = await job_post_body(request)
    except ValueError as e:
        return intake_reply(await intake_sandbox(request), {'error': str(e)[:200]}, 400)
    if not multipart and (blocked := await turnstile.guard(request, call_in(), body)):
        return blocked
    sandbox = await intake_sandbox(request)
    if not os.environ.get('ANTHROPIC_API_KEY'):
        return intake_reply(sandbox, {'error': 'drafting is not set up here'}, 503)
    post = str(body.get('text') or '').strip()
    if not 40 <= len(post) <= 20000:
        return intake_reply(sandbox, {'error': 'paste the whole job post (40 to 20,000 characters)'}, 400)
    ip = turnstile.ip_of(request)
    now = time.time()
    recent = [t for t in DRAFTS.get(ip, []) if now - t < 3600]
    if len(recent) >= 10:
        return intake_reply(sandbox, {'error': 'too many drafts, try again later'}, 429)
    DRAFTS[ip] = [*recent, now]
    try:
        clean, dropped = await jobpost.draft(post)
    except Exception as e:
        return intake_reply(sandbox, {'error': f'the draft failed: {str(e)[:200]}'}, 502)
    return intake_reply(sandbox, await jobpost.save(sandbox, post, clean, dropped, source_file))


async def api_intake_confirm(request):
    sandbox = await intake_sandbox(request)
    job = await jobpost.confirm(sandbox, int(request.match_info['id']))
    return intake_reply(sandbox, job or {'error': 'no draft job with that id here'}, 200 if job else 404)


async def api_intake_resumes(request):
    """Files (PDF, docx, text, .eml, .csv) and pasted text, as multipart. One bad file never stops the rest."""
    # The bot check's token comes in a header, so nothing of the upload is read before it passes
    if blocked := await turnstile.guard(request, call_in()):
        return blocked
    sandbox = await intake_sandbox(request)
    batch, errors, job_id = [], [], None
    reader = await request.multipart()
    while (part := await reader.next()) is not None:
        if not isinstance(part, BodyPartReader):
            continue
        if part.name == 'job_id':
            job_id = int(v) if (v := (await part.text()).strip()).isdigit() else None
        elif part.name == 'text':
            if text := (await part.text()).strip():
                batch.append(extract.pasted(text))
        elif part.name == 'files' and part.filename:
            if len(batch) + len(errors) >= MAX_FILES:
                errors.append({'label': part.filename, 'status': 'error', 'why': f'more than {MAX_FILES} files'})
                continue
            try:
                batch += extract.items(part.filename, await part.read())
            except Exception as e:
                errors.append({'label': part.filename, 'status': 'error', 'why': str(e)[:200]})
    try:
        job = await load.target_job(sandbox, job_id)
    except LookupError as e:
        return intake_reply(sandbox, {'error': str(e)}, 404)
    results = await load.import_all(sandbox, job, batch)
    return intake_reply(sandbox, {'job': {'id': job['id'], 'title': job['title']}, 'results': [*results, *errors]})


async def health(_):
    return web.json_response({'ok': True, 'commit': os.environ.get('COMMIT', 'dev')})


def app() -> web.Application:
    a = web.Application(client_max_size=25 * 2**20)   # intake uploads: a batch of resumes
    a.add_routes([web.get('/intake', intake_page), web.get('/api/intake/jobs', api_intake_jobs),
                  web.post('/api/intake/job-post', api_intake_job_post),
                  web.post('/api/intake/jobs/{id}/confirm', api_intake_confirm),
                  web.post('/api/intake/resumes', api_intake_resumes), web.get('/health', health)])
    return a


if __name__ == '__main__':
    web.run_app(app(), host=os.environ.get('HOST', '127.0.0.1'), port=int(os.environ.get('PORT', '8091')))
