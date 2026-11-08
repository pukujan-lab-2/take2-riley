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

# Each test file's results from this run, saved to eval_runs at the end so the console's Evals shows them over time.
# Also, for the Checks page: each test's seconds, a failure's message, and each judge verdict (evals.judge.LOG)
RESULTS: dict[str, dict[str, bool]] = {}
EXTRA: dict[str, dict[str, dict]] = {}
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
    extra = EXTRA.setdefault(Path(file).stem, {'durations': {}, 'failures': {}})
    extra['durations'][name] = round(extra['durations'].get(name, 0) + report.duration, 2)
    if report.failed:
        extra['failures'][name] = report.longreprtext[-1500:]


def run_detail(file: str, judged: dict[str, list[dict]]) -> dict:
    """What a file's run keeps beside its results: seconds and failures per test, and its judge verdicts."""
    extra = EXTRA.get(file, {})
    details = {node.partition('::')[2]: v for node, v in judged.items() if Path(node.partition('::')[0]).stem == file}
    return {k: v for k, v in (('durations', extra.get('durations')), ('failures', extra.get('failures')),
                              ('details', details)) if v}


def pytest_sessionfinish(session):
    # Only with the Octo workspace set (never in CI, which has no keys); RILEY_NO_EVAL_LOG=1 turns it off
    if not RESULTS or os.environ.get('RILEY_NO_EVAL_LOG'):
        return
    from riley import keys, octo
    keys.load()
    if not octo.ready():
        return

    judge = sys.modules.get('evals.judge')
    judged = judge.LOG if judge else {}

    async def save():
        seconds = round(time.time() - STARTED, 1)
        for file, tests in RESULTS.items():
            await octo.save_eval(f'test-{file}-{secrets.token_hex(3)}', f'test:{file}', all(tests.values()),
                                 {'checks': tests, 'seconds': seconds, **run_detail(file, judged)})
    # A results page that is down never fails the tests
    with contextlib.suppress(Exception):
        asyncio.run(save())
