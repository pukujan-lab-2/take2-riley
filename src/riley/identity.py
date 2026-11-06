"""Who is calling. A known number finds the record; Riley then checks the name on it and the role they applied for
before it says anything about an application.

Until the caller confirms, the agent's instructions hold nothing about them: no resume, no status, no dates. The
confirm_identity tool puts those in, so the model cannot read out what it has not been given. A caller who says
it isn't them hears nothing about any application, not even that one exists.
"""
from collections.abc import Awaitable, Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from livekit.agents import Agent, RunContext, function_tool
from livekit.agents.llm import ToolError, ToolResult

PENDING = """

Before anything else: the number they called from is on file, and you have just asked if this is them and about \
the role they applied for. Wait for their answer, then call confirm_identity. Until it confirms them, you know \
nothing about them: don't ask screening questions, say any detail, or use any other tool."""


def day(dt: datetime | None, tz: str = 'America/New_York') -> str:
    if not dt:
        return ''
    d = dt.astimezone(ZoneInfo(tz))
    return f"{d.strftime('%A, %B')} {d.day}"


def question(who: dict, tz: str = 'America/New_York') -> str:
    """The name on file and the role, in one question. A caller first screened from an unknown number has the
    placeholder name from screen.yaml (agent.unknown_caller_name) until a resume or Dana gives the real one."""
    on = f" on {day(who['applied'], tz)}" if who.get('applied') else ''
    return f"Am I speaking with {who['name']}, about the {who['title']} role you applied for{on}?"


# Why Riley rang, in its first words on a call it placed (riley/outbound.py modes). "Thanks for calling" to someone
# Riley phoned was the first outbound test call's opening (5 Nov). It names no outcome before the identity check.
PLACED = {'outcome': 'calling with an update on your application',
          'follow_up': 'calling with an update on your application',
          'callback': 'calling you back, as you asked'}


def hello(cfg: dict, placed: str | None = None) -> str:
    """The first sentence of a call: who is speaking, and on a call Riley placed (`placed`, its mode), from where
    and why."""
    a = cfg['agent']
    if placed:
        return f"Hi, this is {a['name']}, an AI assistant from {a['employer']} nursing recruitment, {PLACED[placed]}."
    return f"Hi, thanks for calling {a['employer']} nursing recruitment. I'm {a['name']}, an AI assistant."


def greeting(cfg: dict, who: dict, tz: str = 'America/New_York', placed: str | None = None) -> str:
    notice = ' This call is recorded so our recruiter can review it.' if cfg['rules'].get('recording_notice') else ''
    return f"{hello(cfg, placed)}{notice} {question(who, tz)}"


def check(state) -> None:
    """Raise in any tool used before the caller confirmed who they are."""
    if getattr(state, 'identity', 'none') == 'pending':
        raise ToolError('first confirm who they are: ask, then call confirm_identity')


def tool(agent: Agent, full_instructions: str, next_step: str, recruiter: str,
         on_yes: Callable[[], Awaitable[None]] | None = None, quiet: bool = False):
    """confirm_identity for this call. On a yes the agent gets its full instructions, with the caller's records, and
    `on_yes` runs (the outcome call plays Dana's recorded line there). With `quiet`, on_yes said the next line and the
    model adds no reply of its own."""
    @function_tool()
    async def confirm_identity(context: RunContext, is_them: bool) -> str | ToolResult:
        """The caller answered whether they are the person on file and this is about the role they applied for.

        Args:
            is_them: true only if they said yes to both: it is them, and the role is right
        """
        state = context.userdata
        if state.identity != 'pending':
            return 'already answered. Never mention this tool.'
        if not is_them:
            state.identity = 'not_them'
            print('  identity      not confirmed', flush=True)
            return (f"not them. Say nothing about any application, not even whether one exists. Say {recruiter} can "
                    'call this number back, then say goodbye and call end_call. Never mention this tool.')
        state.identity = 'confirmed'
        print('  identity      confirmed', flush=True)
        await agent.update_instructions(full_instructions)
        if on_yes:
            await on_yes()
        if quiet:
            return ToolResult(f'confirmed. {next_step} Never mention this tool.', reply_required=False)
        return f'confirmed. {next_step} Never mention this tool.'
    return confirm_identity
