"""Octo is where every call lives: its record in the workspace's own Postgres, its traces in the workspace's Phoenix.

Workspace `nurse-screen`, reached with NURSE_OCTO_KEY (workspace-scoped: read, write, files, delete) and
NURSE_OCTO_WORKSPACE. Without them the worker still takes calls and only writes calls/<room>.json.
"""
import json
import os
import subprocess
from datetime import datetime

import httpx

URL = os.environ.get('OCTO_URL', 'https://octodb.design-bakery.com/api')
# Cloudflare in front of Octo refuses clients that send no user agent
HEADERS = {'User-Agent': 'nurse-screen'}

SCHEMA = """
create table if not exists calls (
  room text primary key,
  source text not null,            -- phone or bot
  started timestamptz not null,
  seconds int,
  commit text,
  ended_by text,
  scorecard jsonb,
  answers jsonb,
  caller_questions jsonb,
  recruiter_requested text,
  callback text,
  latency jsonb,
  transcript jsonb,
  brain_chain jsonb,
  public_transcript boolean not null default false
);
create table if not exists eval_runs (
  id bigserial primary key,
  room text not null,
  commit text,
  scenario text not null,
  passed boolean not null,
  checks jsonb not null,
  at timestamptz not null default now()
);
create index if not exists calls_started on calls (started desc);
create index if not exists eval_runs_at on eval_runs (at desc);
"""
_migrated = False


def ready() -> bool:
    return bool(os.environ.get('NURSE_OCTO_KEY') and os.environ.get('NURSE_OCTO_WORKSPACE'))


def _auth() -> dict:
    return {**HEADERS, 'Authorization': f"Bearer {os.environ['NURSE_OCTO_KEY']}"}


async def query(sql: str, params: list | None = None, row_limit: int = 500) -> list[dict]:
    # Dates go over JSON as ISO text; the store passes datetimes for applied, promised_by, withdrawn and due
    params = [p.isoformat() if isinstance(p, datetime) else p for p in params or []]
    body = {'sql': sql, 'rowLimit': row_limit, **({'params': params} if params else {})}
    async with httpx.AsyncClient(timeout=20) as http:
        r = await http.post(f"{URL}/workspaces/{os.environ['NURSE_OCTO_WORKSPACE']}/query", headers=_auth(), json=body)
    if r.status_code >= 400:
        raise RuntimeError(f'octo query {r.status_code}: {r.text[:300]}')
    return r.json().get('rows', [])


async def migrate() -> None:
    await query(SCHEMA)


async def save_call(record: dict, source: str, public_transcript: bool) -> None:
    cols = ['room', 'source', 'started', 'seconds', 'commit', 'ended_by', 'scorecard', 'answers', 'caller_questions',
            'recruiter_requested', 'callback', 'latency', 'transcript', 'brain_chain', 'public_transcript']
    values = {**record, 'source': source, 'public_transcript': public_transcript,
              'commit': os.environ.get('COMMIT', 'dev')}
    params = [json.dumps(values[c]) if isinstance(values[c], (dict, list)) else values[c] for c in cols]
    marks = ', '.join(f'to_timestamp(${i})' if c == 'started' else f'${i}' for i, c in enumerate(cols, 1))
    updates = ', '.join(f'{c} = excluded.{c}' for c in cols[1:])
    await migrate()  # create-if-missing, so a fresh workspace needs no setup step
    await query(f'insert into calls ({", ".join(cols)}) values ({marks}) on conflict (room) do update set {updates}',
                params)


def commit() -> str:
    """The commit a run is for: COMMIT where the image sets it, else this checkout's HEAD, else 'dev'."""
    if os.environ.get('COMMIT'):
        return os.environ['COMMIT']
    try:
        out = subprocess.run(['git', 'rev-parse', '--short', 'HEAD'], capture_output=True, text=True, timeout=5,
                             cwd=os.path.dirname(os.path.abspath(__file__)))
        return out.stdout.strip() or 'dev'
    except (OSError, subprocess.SubprocessError):
        return 'dev'


async def save_eval(room: str, scenario: str, passed: bool, result: dict, commit_id: str | None = None) -> None:
    """One test or bot run for the results page. `result['checks']` is {check name: passed}; the rest (waits,
    scorecards, seconds) is kept as it is. Scenario names start with the kind of run: text:, test:, meta:, or a bot
    scenario's own name."""
    global _migrated
    if not _migrated:  # text_run saves thousands of runs; create-if-missing once is enough
        await migrate()
        _migrated = True
    await query('insert into eval_runs (room, commit, scenario, passed, checks) values ($1, $2, $3, $4, $5)',
                [room, commit_id or commit(), scenario, passed, json.dumps(result)])


def tracer(room: str, content: bool = True):
    """An OTLP exporter to the workspace's Phoenix project. Every span carries the room, so a call's spans group."""
    from livekit.agents.telemetry import set_tracer_provider
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({'service.name': 'nurse-screen'}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(
        endpoint=f"{URL}/workspaces/{os.environ['NURSE_OCTO_WORKSPACE']}/otel/v1/traces", headers=_auth())))
    # The conversation goes into the spans unless the business turns it off (screen.yaml records.traces_content)
    set_tracer_provider(provider, metadata={'session.id': room, 'call.room': room}, allow_pii=content)
    return provider
