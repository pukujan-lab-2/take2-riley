"""Bring Riley's latest code over from a nurse-screen checkout, with the changes this repo needs.

  python3 scripts/sync_riley.py ~/dev/projects/nurse-screen [ref]     (ref defaults to origin/main)

Copies agent/ to src/riley/, intake/ to src/intake/, web/session.py and web/tracker/rules.py to src/riley_web/ (so
the intake API resolves the console's sign-in exactly as nurse-screen does), screen.yaml, jobs/ and the tests already
here, then: paths one folder deeper, imports from `riley`, and riley/main.py hosted by src/agent.py with the starter's
turn detector and noise cancellation. src/riley_web/intake.html is no longer synced: nurse-screen retired its
web/intake page for the console's Jobs screen (nurse-screen #84) and only proxies /api/intake/* here.
Fails loudly when a change no longer applies, so a reworked main.py is looked at by hand.
"""
import io
import re
import subprocess
import sys
import tarfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
# nurse-screen's web files the intake API here needs as they are: who a request is (the console's session or the demo
# cookie) and the rules it rests on. Their imports of agent/ and web/tracker/ become riley/ and riley_web/
WEB = {'web/session.py': 'session.py', 'web/tracker/rules.py': 'rules.py'}

MAIN = [
    ('"""The phone worker: LiveKit sends each call to the number (or a browser test room) here as agent `nurse-screen`.',
     '"""Riley\'s calls: src/agent.py (AGENT_PROFILE=riley) hands each call to the number (or a browser test room) here.'),
    ("""Run:  .venv/bin/python -m agent.main dev        (connects to LIVEKIT_URL; call the number or use the Agents Playground)
      .venv/bin/python -m agent.main console    (talk to it with this Mac's mic and speaker, no LiveKit needed)
""", """Run:  AGENT_PROFILE=riley AGENT_NAME=nurse-screen uv run src/agent.py dev   (call the number or use the Agents Playground)
      AGENT_PROFILE=riley uv run src/agent.py console                      (this Mac's mic and speaker)
The starter's pieces are used here as in src/agent.py: the LiveKit Inference turn detector, preemptive generation and
ai_coustics noise cancellation. Riley keeps its own silence check-in (_quiet below), which the hard-caller evals replay.
"""),
    ("""from livekit.agents import AgentServer, AgentSession, JobContext, JobProcess, cli, room_io, stt, tts
from livekit.agents.beta.tools import EndCallTool
from livekit.plugins import cartesia, deepgram, silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel
""", """from livekit.agents import AgentSession, JobContext, JobProcess, inference, room_io, stt, tts
from livekit.agents.beta.tools import EndCallTool
from livekit.plugins import ai_coustics, cartesia, deepgram, silero
"""),
    ("""try:  # LiveKit Cloud's telephony noise filter, when the plugin is installed and the project is on Cloud
    from livekit.plugins import noise_cancellation
except ImportError:
    noise_cancellation = None


""", ''),
    ("""server = AgentServer()


def prewarm(proc: JobProcess):
    proc.userdata['vad'] = silero.VAD.load(min_silence_duration=0.3)


server.setup_fnc = prewarm
""", """def prewarm(proc: JobProcess):
    proc.userdata['vad'] = silero.VAD.load(min_silence_duration=0.3)
"""),
    ("@server.rtc_session(agent_name='nurse-screen')\nasync def entrypoint", 'async def entrypoint'),
    ("            'turn_detection': MultilingualModel(),",
     "            # The starter's turn detector (LiveKit Inference): words and tone together\n"
     "            'turn_detection': inference.TurnDetector(),"),
    ("""    phone = noise_cancellation.BVCTelephony() if noise_cancellation else None
    await session.start(agent=agent, room=ctx.room, room_options=room_io.RoomOptions(
        audio_input=room_io.AudioInputOptions(noise_cancellation=(lambda p: phone) if phone else None)))


if __name__ == '__main__':
    cli.run_app(server)
""", """    # The starter's noise cancellation: background noise and other voices are removed before Deepgram hears them
    await session.start(agent=agent, room=ctx.room, room_options=room_io.RoomOptions(
        audio_input=room_io.AudioInputOptions(
            noise_cancellation=ai_coustics.audio_enhancement(model=ai_coustics.EnhancerModel.QUAIL_VF_S)),
        delete_room_on_close=True))
"""),
]


def apply(text: str, pairs, name: str) -> str:
    for old, new in pairs:
        if old not in text:
            sys.exit(f'{name}: this no longer applies, port it by hand:\n{old[:200]}')
        text = text.replace(old, new)
    return text


def main():
    src, ref = Path(sys.argv[1]).expanduser(), sys.argv[2] if len(sys.argv) > 2 else 'origin/main'
    raw = subprocess.run(['git', '-C', str(src), 'archive', ref, 'agent', 'intake', *WEB, 'screen.yaml', 'jobs',
                          'tests'],
                         check=True, capture_output=True).stdout
    tests = {p.name for p in (HERE / 'tests').glob('*.py')} - {'test_agent.py'}
    with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
        for m in tar.getmembers():
            if not m.isfile():
                continue
            data = tar.extractfile(m).read()
            top, _, rest = m.name.partition('/')
            if top == 'agent' and rest.endswith('.py'):
                out = HERE / 'src' / 'riley' / rest
            elif top == 'intake' and rest.endswith('.py'):
                out = HERE / 'src' / 'intake' / rest
            elif m.name in WEB:
                out = HERE / 'src' / 'riley_web' / WEB[m.name]
            elif top == 'tests' and '/' not in rest and rest in tests:
                out = HERE / 'tests' / rest
            elif m.name == 'screen.yaml' or top == 'jobs':
                out = HERE / m.name
            else:
                continue
            text = data.decode()
            text = re.sub(r'agent/([a-z_]+)\.py', r'riley/\1.py', text)
            if top in ('agent', 'intake') or m.name in WEB:
                text = re.sub(r'from agent(\.| import)', r'from riley\1', text)
                text = text.replace('from web.tracker import rules', 'from riley_web import rules')
                text = text.replace('ROOT = Path(__file__).resolve().parent.parent', 'ROOT = Path(__file__).resolve().parents[2]')
                text = text.replace('checkout_envs(Path(__file__).resolve().parent.parent)',
                                    'checkout_envs(Path(__file__).resolve().parents[2])')
                text = text.replace('from agent import keys', 'from riley import keys')
                if rest == 'main.py':
                    text = apply(text, MAIN, 'riley/main.py')
            if top == 'tests':
                text = re.sub(r'from agent(\.| import)', r'from riley\1', text)
                text = re.sub(r'\bagent\.(main|screener|store|octo|keys|guest|identity|match|outcome|scheduler|signals|'
                              r'helpline|brain|outbound|followup|outcomeline|bias|seed|phone|pro_speech|guest_call)\b',
                              r'riley.\1', text)
                text = text.replace("FIXTURES = Path(__file__).parent / 'fixtures'",
                                    "FIXTURES = Path(__file__).resolve().parents[1] / 'samples'")
                text = text.replace('''    from web import server
    from web.tracker import api
    assert server.SANDBOX_COOKIE == api.COOKIE''', '''    # The tracker stays in pukujan-lab-2/nurse-screen (web/tracker/api.py COOKIE); its web server proxies /intake here
    from riley_web import server
    assert server.SANDBOX_COOKIE == "ns_guest"''')
                if rest == 'conftest.py':
                    text = apply(text, [("sys.path.insert(0, str(Path(__file__).resolve().parent.parent))",
                                         "sys.path.insert(0, str(Path(__file__).resolve().parent.parent))\n"
                                         "sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))")],
                                 'tests/conftest.py')
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text)
    rev = subprocess.run(['git', '-C', str(src), 'rev-parse', '--short', ref], capture_output=True, text=True).stdout
    print('synced Riley from nurse-screen', rev.strip())


if __name__ == '__main__':
    main()
