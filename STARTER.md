# Take2 Voice Agent Hackathon — Starter

Build a voice agent that does **one job on a real phone call**, then demo it live
while a judge plays a difficult caller.

This repo is LiveKit's official
[agent-starter-python](https://github.com/livekit-examples/agent-starter-python)
with a Take2 layer on top: two working example agents, a template for yours,
difficult-caller test scenarios, and phone setup. LiveKit's original README is in
[LIVEKIT_README.md](LIVEKIT_README.md).

**Cost: free.** Everything runs on LiveKit Cloud's free plan. Your LiveKit keys
are the only credentials you need. Speech-to-text, the LLM and text-to-speech all
go through LiveKit Inference, so you don't need Deepgram, Google or Cartesia
accounts. See [What's free](#whats-free-and-how-not-to-run-out).

---

## 0. Before the event (15 minutes, please do this at home)

1. **Create a free LiveKit Cloud account** at [cloud.livekit.io](https://cloud.livekit.io)
   and create a project. No credit card needed. **Every teammate should make their
   own account**: free credits are per person, so more accounts means more runway.
2. **Install tools**
   - Python 3.10–3.14 and [uv](https://docs.astral.sh/uv/getting-started/installation/)
   - LiveKit CLI 2.18.8 or later:
     - macOS: `brew install livekit-cli`
     - Linux: `curl -sSL https://get.livekit.io/cli | bash`
     - Windows: `winget install LiveKit.LiveKitCLI`
   - Check with `lk --version`.
3. **Clone and install**
   ```bash
   git clone https://github.com/vramesh1/take2-voice-agent-hackathon-starter take2-voice-starter
   cd take2-voice-starter
   uv sync
   ```
4. **Connect to your LiveKit project**
   ```bash
   lk cloud auth
   lk app env --write --destination .env.local
   ```
   This writes `LIVEKIT_URL`, `LIVEKIT_API_KEY` and `LIVEKIT_API_SECRET`. Three
   more settings default to `AGENT_PROFILE=booking`, `MODEL_PRESET=budget` and
   `AGENT_NAME=take2-agent`. Add any of them to `.env.local` to change them (see
   `.env.example`).
5. **Talk to it**
   ```bash
   lk agent console
   ```
   Talk to Riley, the dental receptionist, through your laptop mic. If she answers,
   you're ready. Press Ctrl+C to stop.

Optional but recommended: run `lk skills install` so Claude Code, Cursor or Codex
know LiveKit's APIs. `AGENTS.md` in this repo tells them how to work here.

---

## 1. What you're building

Pick one job a business would really put on a phone line. Examples:

- book, move or cancel an appointment (clinic, salon, car service)
- screen a job candidate (the `screener` example)
- take a restaurant order or reservation
- qualify a sales lead, or triage a support call

Rules of the brief:

- **One job, done end to end on a live call.** Narrow and reliable beats broad and flaky.
- **It must answer a real phone number** for the demo (setup in section 4).
- **Any models or extra services are fine**, as long as the demo runs live.
- Teams of 2 to 4. Everything you build is yours to keep.

---

## 2. How you're judged

A panel of three scores each live demo. One judge plays a **difficult caller**.

| Criteria | Weight |
|---|---|
| Did the agent do its job on the live call? | 30 |
| How did it handle a difficult caller (interruptions, silence, off-topic or improper requests)? | 25 |
| How natural did the conversation feel (response time, turn-taking, wording)? | 20 |
| Was the problem clear and were design choices deliberate? | 15 |
| Bonus: anything the team did uniquely well | 10 |

Prepare for the difficult caller. Expect them to interrupt you mid-sentence,
go silent, change their mind, go off-topic, and ask for something your agent
shouldn't do.

---

## 3. The repo

```
src/
  agent.py           entry point: pipeline, silence handling, picks the agent
  models.py          model presets (budget | quality); swap models here
  prompts.py         shared voice rules + difficult-caller rules
  booking_agent.py   example: dental receptionist, books appointments
  screener_agent.py  example: recruiter, runs a 4-question phone screen
  my_agent.py        YOUR agent; start here
scenarios.yaml           difficult-caller simulations for the booking agent
scenarios-screener.yaml  simulations for the screener agent
dispatch-rule.json       routes phone calls to your agent
```

### How a turn works

```
caller audio ─▶ noise cancellation ─▶ STT ─▶ turn detector ─▶ LLM (+ tools) ─▶ TTS ─▶ caller
                                              "are they done?"
```

- **STT** (speech-to-text) turns audio into text. Default: AssemblyAI.
- **Turn detector** decides when the caller has finished speaking, from both words
  and tone. It is the biggest single factor in feeling natural.
- **LLM** decides what to say and when to call tools. Default: Gemma 4.
- **TTS** (text-to-speech) speaks the reply. Default: Fish Audio.
- **Preemptive generation** starts drafting the reply before the turn is confirmed.
- **Adaptive interruptions** (on by default) tell a real interruption from "mhm".
- **Silence handling** (`agent.py`): after 8 quiet seconds the agent checks in,
  and after two check-ins it says goodbye and hangs up.
- **`end_call` tool**: every example can hang up when the job is done.

### Build your agent

1. Open `src/my_agent.py` and fill in `JOB_PROMPT`: who the agent is, numbered
   steps, what done looks like, and what it must refuse.
2. Replace `save_request` with tools for the actions your agent takes. **The
   docstring is the instruction the LLM reads**, so write it carefully.
3. Set `AGENT_PROFILE=mine` in `.env.local`.
4. Run `lk agent console` and talk to it.

Look at `booking_agent.py` and `screener_agent.py` for the full pattern.

### Fast iteration without talking

Talking out loud is slow. Use the text debugger, which prints each reply and the
tool calls behind it:

```bash
lk agent debugger start
lk agent debugger say "Hi, I need to book a cleaning for tomorrow"
lk agent debugger restart      # after every code change
lk agent debugger stop
```

### Test against difficult callers

Simulated callers run a whole conversation, then an LLM judge grades it:

```bash
lk agent simulate text --scenarios scenarios.yaml             # booking agent
lk agent simulate text --scenarios scenarios-screener.yaml    # screener agent
```

Set `AGENT_PROFILE` in `.env.local` to match the scenarios file. For your own
agent, copy `scenarios.yaml`, keep the same kinds of caller, and rewrite them for
your job. Simulations spend credits, so run them before milestones, not on every edit.

### Change models

Set `MODEL_PRESET=quality` for Deepgram Nova-3 + Gemini 3.1 Flash Lite + Cartesia
Sonic 3, or edit `src/models.py`. Every option is in
[LiveKit's model docs](https://docs.livekit.io/agents/models/). Model IDs follow
the pattern `provider/model`, for example `deepgram/flux-general-en`.

---

## 4. Put it on a phone number

Each free LiveKit account includes **one US phone number and 50 inbound minutes**.
Do this by the 6:00 PM milestone; don't leave it for demo time.

1. **Rent the number.** In the [LiveKit Cloud dashboard](https://cloud.livekit.io)
   go to **Telephony → Phone Numbers → Rent a number**, search an area code
   (for example 212, 646, 917), and rent it. Or from the CLI:
   ```bash
   lk number search --country-code US --area-code 646
   lk number purchase --numbers +1646XXXXXXX
   ```
2. **Create the dispatch rule.** This tells LiveKit which agent answers. The
   `agent_name` in the file must match `AGENT_NAME` in `.env.local`.
   ```bash
   lk sip dispatch create dispatch-rule.json
   ```
   Or in the dashboard: **Telephony → Dispatch rules → Create new dispatch rule →
   JSON editor**, then paste the object that starts at `"rule"`.
3. **Attach the rule to the number.**
   ```bash
   lk number list                      # copy the phone number ID
   lk sip dispatch list                # copy the dispatch rule ID
   lk number update --id <PHONE_NUMBER_ID> --sip-dispatch-rule-id <DISPATCH_RULE_ID>
   ```
   Or in the dashboard: Phone Numbers → **⋮** next to your number → **Assign dispatch rule**.
4. **Run the agent connected to LiveKit Cloud** (console mode does not take calls):
   ```bash
   lk agent dev --deployment production
   ```
   The `--deployment production` flag matters. Plain `lk agent dev` registers your
   laptop under a random `dev-…` name, which the Agent Console can reach but phone
   calls can't. `uv run src/agent.py dev` also works.
5. **Call the number from your phone.** Your agent should answer within a couple of seconds.

Run **only one** agent this way per LiveKit project. If two teammates both run it
against the same project, calls go to whichever laptop LiveKit picks.

The phone rings but nobody answers? Then the agent isn't running with
`--deployment production`, or the `agent_name` in the dispatch rule doesn't
match `AGENT_NAME`.

---

## 5. Milestones

| Time | Milestone |
|---|---|
| 4:00 PM | Build starts. Everyone on the team has run `lk agent console` successfully. |
| 5:00 PM | **Talking:** your agent's prompt runs in console and does the happy path. |
| 6:00 PM | **Calling:** your tools work, and your agent answers your phone number. |
| 7:00 PM | **Demo-ready:** handles interruptions, silence, off-topic and improper requests; scenarios pass; 2-minute pitch rehearsed. |

Mentors from Take2 will come around at each milestone. Flag one down any time.

### Demo format

Live demo on a real call, about 10 minutes per team:

1. Explain the problem and who calls this number, in about one minute.
2. Make a normal call that completes the job.
3. A judge calls as a difficult caller.
4. Explain one or two design choices and why you made them.

---

## What's free, and how not to run out

LiveKit Cloud's free **Build** plan (no credit card) includes, per person, per month:

- **$2.50 of LiveKit Inference credits** for STT, LLM and TTS together
- 1 US local phone number and 50 inbound call minutes
- 5,000 WebRTC minutes, and 1,000 agent session minutes for agents deployed to
  LiveKit Cloud (`lk agent dev` on your laptop doesn't use these)
- Up to 5 concurrent sessions

Free limits are **hard caps**: when you hit one, requests fail. You are never
charged. Rough runway from $2.50, by our estimate:

| Preset | Cost per conversation minute | Minutes per account |
|---|---|---|
| `budget` (default) | ~$0.015 | ~150 |
| `quality` | ~$0.03 | ~80 |

Text-to-speech is usually the biggest cost. Realtime speech-to-speech models cost
the most, so try them last. Each call's usage is logged when the call ends
(`session usage:` in your terminal).

**If you run out:** switch `.env.local` to a teammate's keys
(`lk app env --write --destination .env.local` from their login), or ask a Take2
mentor for backup keys.

---

## When something breaks

| Symptom | Fix |
|---|---|
| `api_key is required` | `.env.local` is missing or empty. Re-run `lk app env --write --destination .env.local`. |
| `lk: command not found` / no `debugger` or `simulate` command | Install or update the LiveKit CLI to 2.18.8 or later. |
| Agent never speaks first | Check the terminal for an error in `on_enter`. A typo in your prompt or tool usually shows here. |
| Agent talks over the caller or cuts them off | Shorten replies in your prompt first. Then try `deepgram/flux-general-en` for STT. |
| Long pause before each reply | Fewer tools, a shorter prompt, and a faster LLM. Check model timing in the [Agent Console](https://cloud.livekit.io/projects/p_/agents/console). |
| Tool never gets called | Rewrite the tool's docstring to say exactly when to call it, and mention the tool's step in `JOB_PROMPT`. |
| Phone rings, no answer | Run `lk agent dev --deployment production` (not plain `lk agent dev`). Check that `agent_name` matches. Run `lk number list` and check the dispatch rule column. |
| Requests start failing mid-session | You're probably out of free credits. See above. |

More: the [LiveKit Agents docs](https://docs.livekit.io/agents/) or `lk docs search "<question>"`.
