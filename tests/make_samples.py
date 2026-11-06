"""Writes Dana's three sample emails to docs/samples/ from `outcome.email`, with made-up records.

Run: .venv/bin/python -m tests.make_samples       (tests/test_outcome.py checks the files still match)
"""
from riley import outcome

# file -> (kind, other roles that fit)
SAMPLES = {
    'next-round.txt': ('next_round', ()),
    'not-yet.txt': ('not_yet', ()),
    'not-selected-other-role.txt': ('not_selected', None),  # None: the telemetry role from tests/test_outcome.py
}


def render(kind: str, parts: dict) -> str:
    app = {'title': parts['role'], 'name': parts['name']}
    e = outcome.email({'kind': kind, 'parts': parts, 'text': outcome.template(parts)}, app)
    return f"Subject: {e['subject']}\nFrom: Dana at Hudson Park Health <dana@hudsonpark.example>\n\n{e['text']}"


if __name__ == '__main__':
    from tests.test_outcome import TELE, parts
    for name, (kind, offers) in SAMPLES.items():
        p = parts(kind, offers=(TELE,) if offers is None else offers)
        (outcome.ROOT / 'docs' / 'samples' / name).write_text(render(kind, p))
        print('wrote', name)
