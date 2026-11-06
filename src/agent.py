"""Entry point: wires the voice pipeline and picks which agent answers.

You mostly edit my_agent.py. Come here to change models, silence handling or
turn-taking.

  .env.local
    AGENT_PROFILE = booking | screener | mine | riley   (which agent answers)
    MODEL_PRESET  = budget  | quality             (see models.py)
    AGENT_NAME    = take2-agent                   (must match your dispatch rule)
"""

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable

from dotenv import load_dotenv
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    STTContextOptions,
    TurnHandlingOptions,
    UserStateChangedEvent,
    cli,
    inference,
    room_io,
)
from livekit.plugins import ai_coustics

import riley_agent
from booking_agent import BookingAgent
from models import load_models
from my_agent import MyAgent
from screener_agent import ScreenerAgent

logger = logging.getLogger("agent")

load_dotenv(".env.local")

AGENTS: dict[str, type[Agent]] = {
    "booking": BookingAgent,
    "screener": ScreenerAgent,
    "mine": MyAgent,
    "riley": riley_agent.RileyAgent,
}

# Profiles that build their own session: Riley looks the caller up before it says a word and picks the agent from
# that (see riley_agent.py). They use the same turn detector and noise cancellation as the entrypoint below.
SESSIONS: dict[str, Callable[[JobContext], Awaitable[None]]] = {
    "riley": riley_agent.entrypoint,
}

AGENT_NAME = os.getenv("AGENT_NAME", "take2-agent")

# Silence: after this many quiet seconds the caller counts as "away".
SILENCE_SECONDS = 8.0
# Check in this many times, then say goodbye and hang up.
MAX_CHECK_INS = 2


def profile_name() -> str:
    return os.getenv("AGENT_PROFILE", "booking").strip().lower()


def pick_agent() -> Agent:
    profile = profile_name()
    if profile not in AGENTS:
        raise ValueError(
            f"Unknown AGENT_PROFILE '{profile}'. Choose one of: {', '.join(AGENTS)}"
        )
    return AGENTS[profile]()


def handle_silence(session: AgentSession) -> None:
    """Ask "are you still there?" when the caller goes quiet, then hang up."""
    check_ins = 0
    tasks: set[asyncio.Task[None]] = set()

    async def check_in() -> None:
        nonlocal check_ins
        check_ins += 1
        if check_ins <= MAX_CHECK_INS:
            await session.generate_reply(
                instructions="The caller has gone quiet. In a few words, ask if they are still there."
            )
        else:
            await session.generate_reply(
                instructions="The caller is not responding. Say a short, polite goodbye."
            )
            session.shutdown()

    @session.on("user_state_changed")
    def _on_user_state(ev: UserStateChangedEvent) -> None:
        nonlocal check_ins
        if ev.new_state == "speaking":
            check_ins = 0
        elif ev.new_state == "away":
            task = asyncio.create_task(check_in())
            tasks.add(task)
            task.add_done_callback(tasks.discard)


server = AgentServer()
if profile_name() == "riley":
    # Riley's Silero VAD, loaded once per worker process
    server.setup_fnc = riley_agent.prewarm


@server.rtc_session(agent_name=AGENT_NAME)
async def entrypoint(ctx: JobContext) -> None:
    ctx.log_context_fields = {"room": ctx.room.name}
    if profile_name() in SESSIONS:
        await SESSIONS[profile_name()](ctx)
        return
    models = load_models()

    session = AgentSession(
        # Ears: speech-to-text
        stt=models.stt,
        # Bias transcription toward words it would otherwise misspell.
        stt_context_options=STTContextOptions(
            keyterms=["Take2", "LiveKit"],
            keyterm_detection={"enabled": True},
        ),
        # Brain: the LLM
        llm=models.llm,
        # Voice: text-to-speech
        tts=models.tts,
        expressive=models.expressive,
        turn_handling=TurnHandlingOptions(
            # Decides when the caller has finished speaking, using both the words
            # and the audio (pitch, pauses). Much better than a fixed silence timer.
            turn_detection=inference.TurnDetector(),
            # Start drafting a reply before the turn is confirmed. Cuts latency.
            preemptive_generation={"enabled": True},
        ),
        user_away_timeout=SILENCE_SECONDS,
    )

    handle_silence(session)

    async def log_usage() -> None:
        # Shows model usage per call so you can see what you're spending.
        logger.info("session usage: %s", session.usage)

    ctx.add_shutdown_callback(log_usage)

    await session.start(
        agent=pick_agent(),
        room=ctx.room,
        room_options=room_io.RoomOptions(
            audio_input=room_io.AudioInputOptions(
                # Removes background noise and other voices before STT hears it.
                noise_cancellation=ai_coustics.audio_enhancement(
                    model=ai_coustics.EnhancerModel.QUAIL_VF_S
                ),
            ),
            # Hang up the phone line when the session ends.
            delete_room_on_close=True,
        ),
    )

    await ctx.connect()


if __name__ == "__main__":
    cli.run_app(server)
