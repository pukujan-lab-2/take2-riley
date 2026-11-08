"""A throwaway Postgres for the tests that must run real SQL (tests/test_tenancy.py), standing in for Octo.

`octo.query` sends SQL with $1-style parameters to the workspace's Postgres over HTTP and gets JSON rows back. Here
the same SQL runs on a local server started in a temporary folder, with rows shaped as Octo returns them
(timestamps as ISO text). It needs Postgres's own programs (initdb, pg_ctl): on the GitHub runner they ship with
the image, on a Mac Postgres.app has them. Without them the tests that need it are skipped, unless
RILEY_REQUIRE_PG=1 (set in CI), which makes a missing server a failure instead.
"""
import contextlib
import datetime as dt
import decimal
import glob
import os
import re
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path

PLACES = ['/usr/lib/postgresql/*/bin', '/Applications/Postgres.app/Contents/Versions/latest/bin',
          '/opt/homebrew/opt/postgresql*/bin', '/usr/local/opt/postgresql*/bin']


def bin_dir() -> Path | None:
    if found := shutil.which('initdb'):
        return Path(found).parent
    for pattern in PLACES:
        for d in sorted(glob.glob(pattern), reverse=True):
            if (Path(d) / 'initdb').exists():
                return Path(d)
    return None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class Server:
    """initdb and pg_ctl in a temporary folder; stop() removes it all."""

    def __init__(self, bins: Path):
        self.bins = bins
        self.dir = Path(tempfile.mkdtemp(prefix='riley-pg-'))
        self.port = _free_port()
        data = self.dir / 'data'
        subprocess.run([bins / 'initdb', '-D', data, '-U', 'riley', '--auth=trust', '-E', 'UTF8', '--no-locale'],
                       check=True, capture_output=True)
        subprocess.run([bins / 'pg_ctl', '-D', data, '-l', self.dir / 'log', '-w', '-o',
                        f"-p {self.port} -c unix_socket_directories='' -c listen_addresses=127.0.0.1 -c fsync=off",
                        'start'],
                       check=True, capture_output=True)
        self.dsn = f'host=127.0.0.1 port={self.port} user=riley dbname=postgres'

    def stop(self) -> None:
        with contextlib.suppress(Exception):
            subprocess.run([self.bins / 'pg_ctl', '-D', self.dir / 'data', '-m', 'immediate', 'stop'],
                           check=True, capture_output=True)
        shutil.rmtree(self.dir, ignore_errors=True)


_PARAM = re.compile(r'\$(\d+)')


def _shape(v):
    """A value as Octo's JSON carries it."""
    if isinstance(v, dt.datetime | dt.date):
        return v.isoformat()
    if isinstance(v, decimal.Decimal):
        return float(v)
    return v


def runner(dsn: str):
    """An async stand-in for octo.query(sql, params, row_limit) on the local server."""
    import psycopg

    conn = psycopg.connect(dsn, autocommit=True)

    async def query(sql: str, params: list | None = None, row_limit: int = 500) -> list[dict]:
        params = [p.isoformat() if isinstance(p, dt.datetime) else p for p in params or []]
        with conn.cursor() as cur:
            if params:
                # psycopg takes %(pN)s; a literal % in the SQL (a like pattern) is doubled first
                cur.execute(_PARAM.sub(lambda m: f'%(p{m.group(1)})s', sql.replace('%', '%%')),
                            {f'p{i}': p for i, p in enumerate(params, 1)})
            else:
                cur.execute(sql)
            if cur.description is None:
                return []
            names = [c.name for c in cur.description]
            return [dict(zip(names, (_shape(v) for v in row), strict=True)) for row in cur.fetchmany(row_limit)]

    query.conn = conn  # type: ignore[attr-defined]
    return query


def required() -> bool:
    return os.environ.get('RILEY_REQUIRE_PG') == '1'
