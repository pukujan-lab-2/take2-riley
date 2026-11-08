# Riley

Riley is an AI phone screener for nurse hiring. A nurse applies with the resume they already have, and Riley calls
them for a short screen: it checks what the resume says, asks only what's missing, and tells them when they'll hear
back. A human recruiter makes the decision.

I built it at the Take2 AI voice agent hackathon on Nov 6, 2026 and put it in production the same week. The hospital
(Hudson Park Hospital) and the recruiter (Dana) are made up for the demo.

- **Try it:** [riley.design-bakery.com/try](https://riley.design-bakery.com/try) (Riley calls you)
- **Call it:** +1 631 709 0065
- **Watch calls live:** [riley.design-bakery.com/live](https://riley.design-bakery.com/live)

## Why

Before the event I interviewed 30 healthcare workers, from nurse aides to RNs. They kept naming the same three problems:

1. **Waiting without knowing.** They don't know if or when they'll hear back, and they often never do.
2. **Forms are unpaid work.** They retype a resume they already have, with a new account for every employer.
3. **Rejections give no reason.** "We went with another candidate" teaches them nothing.

Recruiters have the opposite problem: too many applications and only seconds to read each one. Riley tries to help
both sides at once.

## What happens on a call

1. Riley greets the caller and says how to ask for a person or another way to do the screen.
2. It confirms what the resume already says in one line, instead of asking it all again.
3. It asks only the questions the resume doesn't answer.
4. If a required item is missing (here, an active New York RN license), it says so plainly. It never rejects anyone.
5. Before hanging up, it reads back what the hiring team has and the date they'll hear by.
6. Dana, the recruiter, reads the result and decides.

Candidates can call the same number any time to check their status. Those calls never change their score.

## How it works

```
caller ─▶ phone number (Telnyx) ─▶ LiveKit Cloud ─▶ Riley worker (on a VPS)
                                                     ├─ listens:  Deepgram   (backup: own speech server)
                                                     ├─ thinks:   Claude Haiku (backup: Sonnet, Gemini, DeepSeek)
                                                     └─ speaks:   Cartesia   (backup: own speech server)
                                                     │
                                                     ├─▶ Octo: stores every call, candidate and answer
                                                     └─▶ Phoenix: a trace of every turn
```

Each part has a fallback, so one provider going down doesn't drop the call. Which models it uses, the job facts and
the screen questions all live in [`screen.yaml`](screen.yaml), so you can change them without touching code.

## What's in this repo

| Path | What it is |
|---|---|
| `src/riley_agent.py`, `src/riley/` | Riley itself: the call, scoring, help line, follow-up calls, scheduler |
| `src/riley_web/`, `src/intake/` | The intake API behind the console's Jobs screen: upload resumes, an application email or a job board CSV, and they become prefilled answers in the asker's own sandbox. Who is asking (a signed-in org or the demo guest) comes from `riley_web/session.py`, synced from nurse-screen |
| `screen.yaml` | The job, the questions, the models and what gets stored |
| `scenarios-riley.yaml` | Simulated difficult callers that test Riley end to end |
| `tests/` | Unit and caller tests |
| `samples/` | A sample resume (PDF, docx, text), an application email, a job post and a board export to try intake with |
| `deploy/` | How it runs on the VPS |

The recruiter pages (`/tracker`, `/console`, `/deck`) live in a separate repo.

## Run it yourself

You need [uv](https://docs.astral.sh/uv/), the [LiveKit CLI](https://docs.livekit.io/home/cli/) and a LiveKit Cloud
project.

```bash
uv sync
cp .env.example .env.local     # add your LiveKit URL, key and secret
```

Riley also reads these keys if you have them: `ANTHROPIC_API_KEY`, `DEEPGRAM_API_KEY`, `CARTESIA_API_KEY`, and
optionally `GOOGLE_API_KEY`, `INFERHUB_API_KEY`, and `NURSE_OCTO_KEY` with `NURSE_OCTO_WORKSPACE` for storage.

Talk to it in your terminal:

```bash
AGENT_PROFILE=riley AGENT_NAME=nurse-screen lk agent console
```

Run the simulated callers:

```bash
AGENT_PROFILE=riley AGENT_NAME=nurse-screen lk agent simulate text --scenarios scenarios-riley.yaml
```

Open the intake page at http://127.0.0.1:8091/intake (its API answers 401 until the browser has a demo or sign-in
cookie from nurse-screen's console):

```bash
cd src && uv run python -m riley_web.server
```

Run the tests (the caller tests call real models, so they need keys and cost a little):

```bash
uv run pytest tests
```

## Pipeline

Merging to `main` deploys in about 3 minutes. The VPS builds an image tagged with the commit, swaps the worker to it,
and rolls back automatically if the new worker doesn't come up.

## Credits

This started from the Take2 hackathon starter, which is built on LiveKit's
[agent-starter-python](https://github.com/livekit-examples/agent-starter-python) (MIT, see [LICENSE](LICENSE)). The
original starter guide is in [STARTER.md](STARTER.md).
