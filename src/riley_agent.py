"""Riley, the nurse-screening phone agent (AGENT_PROFILE=riley), moved here from pukujan-lab-2/nurse-screen.

Riley screens nurses who call +1 631 709 0065 for Hudson Park Health, calls back the ones who asked, and calls the
guests who sign up on riley.design-bakery.com/try. Unlike the examples, which agent answers depends on the caller:
a new caller gets the screen, a number on file gets the help line or its outcome, and a call Riley placed carries
its reason in the dispatch metadata. So this profile builds its own session in riley/main.py, before the call's
first word, with the same turn detector and noise cancellation as the other profiles.

  riley/screener.py   the screen: questions, recorded answers, the scorecard
  riley/brain.py      the LLM fallback chain from screen.yaml stack.brain (Haiku, Sonnet, Gemini, DeepSeek)
  riley/store.py      the hiring records in the nurse-screen Octo workspace; riley/octo.py adds Phoenix traces
  riley/outbound.py   calls Riley places through the riley-telnyx SIP trunk
  riley/guest.py      the /try guests
  screen.yaml         the job, the rules and the model chains (re-read for every call)

Keep AGENT_NAME=nurse-screen: the number's dispatch rule and riley/outbound.py both name that agent.
"""

from riley.main import entrypoint, prewarm
from riley.screener import Screener

# The agent a plain screen uses, for tools that want the class (riley/main.py picks it per call)
RileyAgent = Screener

__all__ = ["RileyAgent", "entrypoint", "prewarm"]
