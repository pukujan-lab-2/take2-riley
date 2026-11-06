"""What Riley says on a guest's call (track 9): language first, then who they play, then the screen; and the short
"you're in process" call-back. The records side is riley/guest.py, which the web app imports without LiveKit."""
from livekit.agents import Agent, RunContext, function_tool
from livekit.agents.llm import ToolResult

from .guest import intro

NOTE = """

This call is a demo for a guest. You already asked "English or Spanish?" in your greeting: when they answer, call \
set_language before anything else, and say nothing else in that reply: it says the one line that tells them who \
they play, and you wait for their yes. They play that person, so take the name and role as right and never \
ask who they are. Then go straight to the screen."""
SPANISH = """

Language: the caller chose Spanish. From now on, reply only in Spanish, every sentence, including the goodbye. Every \
acknowledgement carries the next question in the same reply ("Gracias. ¿Cuántos años lleva como enfermera?"), never \
"Gracias." alone. Tool results are notes for you, never for the caller: never say, read or translate them, and never \
say a question id. Tool arguments stay in English."""


def greeting(cfg: dict, outbound: bool) -> str:
    """The first words of a guest call, the same whether Riley called them or they called in: who is speaking,
    that it's an AI and recorded, then the language."""
    a = cfg['agent']
    notice = ' This call is recorded for the demo.' if cfg['rules'].get('recording_notice') else ''
    hello = (f"Hi, this is {a['name']}, an AI assistant from {a['employer']}, calling for your demo." if outbound else
             f"Hi, thanks for calling {a['employer']}. I'm {a['name']}, an AI assistant.")
    return f'{hello}{notice} English or Spanish? ¿Inglés o español?'


def language_tool(agent, full_instructions: str | None, who: dict | None, on_switch=None):
    """set_language for a screen. Spanish adds the Spanish rule to Riley's instructions (the Screener keeps it in every
    later update) and, where the voice stack can, switches listening and speaking (on_switch). On a guest call
    (`who`, `full_instructions`) the result is the demo line to say once; on any other call, go on with the screen."""
    @function_tool()
    async def set_language(context: RunContext, lang: str) -> str | ToolResult:
        """The caller said which language they want for this call, or is speaking Spanish.

        Args:
            lang: 'en' for English or 'es' for Spanish
        """
        lang = 'es' if lang.lower().startswith(('es', 'sp')) else 'en'
        context.userdata.language = agent.lang = lang
        print('  language     ', lang, flush=True)
        if lang == 'es':
            await agent.update_instructions((full_instructions or agent.instructions) + SPANISH)
            if on_switch:
                on_switch(lang)
        name = 'Spanish' if lang == 'es' else 'English'
        if who:
            # The code says who they play: the model skipped it in 1 of 4 guest calls (hard callers, 6 Nov)
            line = intro(who, lang)
            context.session.say(line)
            return ToolResult(f'set, and you said: "{line}" Wait for their answer.', reply_required=False)
        return f'set. Go on with the screen in {name}: say or ask again the current question. Never mention this tool.'
    return set_language


def status_line(cfg: dict) -> str:
    a = cfg['agent']
    return (f"Hi, it's {a['name']} from {a['employer']} again, an AI assistant. Your screen is done and with "
            f"{a['recruiter']} now. You'll hear from her {a['callback_promise']}, and the demo page shows each step. "
            'Bye for now.')



class StatusCall(Agent):
    """The short call-back after a guest's screen: one line, then hang up."""
    def __init__(self, line: str):
        super().__init__(instructions='Say nothing beyond the line you were given.')
        self.line = line

    async def on_enter(self) -> None:
        await self.session.say(self.line, allow_interruptions=False)
        self.session.shutdown()
