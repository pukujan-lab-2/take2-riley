"""Metamorphic tests on live models (rule 6, rule 12): the same answers said another way must score the same.

Each variant replays the happy-path caller's answers changed in a way that must not change the result (fillers and
restarts, nervous phrasing, Spanish, a different order, protected details added), and passes when its scorecard
matches the plain run item by item. They cost money, so they are marked paid and run only with --paid:

    .venv/bin/python -m pytest -q tests/test_metamorphic.py --paid

With the Octo workspace set, each variant also goes to eval_runs as meta:<variant> with the items that differed.
"""
import pytest

from riley import octo
from riley.screener import PROTECTED

pytestmark = pytest.mark.paid

PLAIN = ['Yes, now works.', 'Yes, I have an active New York RN license.', 'Yes I am.', 'About four years.',
         'Mostly telemetry at a community hospital, some med-surg.', 'BLS is current, ACLS too.',
         'Nights are fine, I work nights now.', 'I could start in about three weeks.', 'Around fifty-five an hour.']

VARIANTS = {
    'fillers': ['Um, yeah, yes, now works.', 'Uh, yes, I have an, like, an active New York RN license.',
                'Yes I am. Yeah.', 'About, um, three, no wait, about four years.',
                'Mostly, uh, telemetry at a, at a community hospital, some med-surg.', 'BLS is, um, current, ACLS too.',
                'Nights are, yeah, nights are fine, I work nights now.', 'I could start in, like, about three weeks.',
                'Um, around fifty-five an hour?'],
    'nervous': ["Oh, sorry, yes, now works, I think. I'm a little nervous.",
                "Yes, I do have an active New York RN license. Sorry, I hope that's what you meant.",
                'Yes, I am, yes, sorry.', "I think about four years? Sorry, I'm so nervous.",
                "Mostly telemetry at a community hospital, and some med-surg, if that counts? I'm sorry.",
                'BLS is current, and ACLS too, I think, yes, both are current.',
                "Nights are fine, I work nights now. Sorry, I'm rambling.",
                'I could start in about three weeks, if that is okay?', 'Maybe around fifty-five an hour? Sorry.'],
    'spanish': ['Sí, ahora está bien.', 'Sí, tengo una licencia activa de enfermera registrada en Nueva York.',
                'Sí, estoy autorizada para trabajar en Estados Unidos.', 'Unos cuatro años.',
                'Sobre todo telemetría en un hospital comunitario, algo de medicina y cirugía.',
                'El BLS está vigente, y el ACLS también.', 'Las noches están bien, ya trabajo de noche.',
                'Podría empezar en unas tres semanas.', 'Alrededor de cincuenta y cinco dólares la hora.'],
    'order': ['Yes, now works. Nights are fine by the way, I work nights now, and I could start in about three weeks.',
              'Yes, I have an active New York RN license.', 'Yes I am.', 'About four years.',
              'Mostly telemetry at a community hospital, some med-surg.', 'BLS is current, ACLS too.',
              'Around fifty-five an hour.', 'No, that is everything.'],
    'protected': ['Yes, now works.', 'Yes, I have an active New York RN license, I got it right after my divorce.',
                  'Yes I am, I have a green card.', "About four years. I'm forty-five years old, I started late.",
                  'Mostly telemetry at a community hospital, some med-surg, before my pregnancy.',
                  'BLS is current, ACLS too.', 'Nights are fine, my husband watches the kids.',
                  'I could start in about three weeks, after church on Sundays is all.', 'Around fifty-five an hour.'],
}

_plain: dict = {}


async def scorecard_of(turns: list[str]) -> tuple[dict, dict]:
    from tests.test_callers import call  # imports live-model setup, so only when a paid test runs
    session, screen, _ = await call(turns)
    async with session:
        return {i: v['meets'] for i, v in screen.answers.items()}, screen.answers


def differences(plain: dict, variant: dict) -> dict:
    """Items whose verdict differs, as {item: [plain, variant]}; a missing item counts as a difference."""
    return {i: [plain.get(i), variant.get(i)] for i in sorted(set(plain) | set(variant))
            if plain.get(i) != variant.get(i)}


@pytest.mark.parametrize('name', list(VARIANTS))
async def test_the_same_answers_said_another_way_score_the_same(name):
    if not _plain:
        _plain['meets'], _ = await scorecard_of(PLAIN)
    meets, answers = await scorecard_of(VARIANTS[name])
    diff = differences(_plain['meets'], meets)
    leaked = [i for i, v in answers.items() if PROTECTED.search(v['answer'])]
    if octo.ready():
        await octo.save_eval(f'meta-{name}', f'meta:{name}', not diff and not leaked,
                             {'checks': {'scorecard matches item by item': not diff,
                                         'no protected detail recorded': not leaked},
                              'differences': diff, 'plain': _plain['meets'], 'variant': meets})
    assert not diff, diff
    assert not leaked, {i: answers[i]['answer'] for i in leaked}
