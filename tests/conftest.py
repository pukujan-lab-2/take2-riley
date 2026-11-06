import asyncio
import contextlib
import os
import secrets
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

# Each test file's results from this run, saved to eval_runs at the end so /results shows tests over time
RESULTS: dict[str, dict[str, bool]] = {}
STARTED = time.time()


def pytest_addoption(parser):
    parser.addoption('--paid', action='store_true', help='also run the paid metamorphic tests (live models)')


def pytest_collection_modifyitems(config, items):
    # The metamorphic tests call live models and cost money, so they run only when asked for (never in CI)
    if config.getoption('--paid'):
        return
    skip = pytest.mark.skip(reason='paid: run with --paid')
    for item in items:
        if 'paid' in item.keywords:
            item.add_marker(skip)


def pytest_runtest_logreport(report):
    if report.skipped or (report.when != 'call' and report.passed):
        return
    file, _, name = report.nodeid.partition('::')
    tests = RESULTS.setdefault(Path(file).stem, {})
    tests[name] = tests.get(name, True) and report.passed


def pytest_sessionfinish(session):
    # Only with the Octo workspace set (never in CI, which has no keys); RILEY_NO_EVAL_LOG=1 turns it off
    if not RESULTS or os.environ.get('RILEY_NO_EVAL_LOG'):
        return
    from riley import keys, octo
    keys.load()
    if not octo.ready():
        return

    async def save():
        seconds = round(time.time() - STARTED, 1)
        for file, tests in RESULTS.items():
            await octo.save_eval(f'test-{file}-{secrets.token_hex(3)}', f'test:{file}', all(tests.values()),
                                 {'checks': tests, 'seconds': seconds})
    # A results page that is down never fails the tests
    with contextlib.suppress(Exception):
        asyncio.run(save())
