"""Keys come from the environment. On this Mac they are read from ~/Documents/projects/.env under the owner's names.

Values are never printed: `python -m agent.keys` shows which names are present.
"""
import os
from pathlib import Path

# owner's name in ~/Documents/projects/.env -> the name the code reads
NAMES = {
    'anthropic_key': 'ANTHROPIC_API_KEY',
    'anthropic_workspace_id': 'ANTHROPIC_WORKSPACE_ID',
    'inferhub_lease': 'INFERHUB_API_KEY',
    'google_api_key': 'GOOGLE_API_KEY',
    'deepgram_api_key': 'DEEPGRAM_API_KEY',
    'cartesia_api_key': 'CARTESIA_API_KEY',
    'livekit_url': 'LIVEKIT_URL',
    'livekit_api_key': 'LIVEKIT_API_KEY',
    'livekit_api_secret': 'LIVEKIT_API_SECRET',
    'octo_db_nurse_screen': 'NURSE_OCTO_KEY',
    'octo_db_nurse_screen_workspace': 'NURSE_OCTO_WORKSPACE',
    # Outbound calls (track 9): the owner's LiveKit trunk riley-telnyx and its Telnyx number as caller id
    'LIVEKIT_OUTBOUND_TRUNK': 'SIP_OUTBOUND_TRUNK_ID',
    'TELNYX_NUMBER': 'RILEY_CALLER_ID',
    'TURNSTILE_SECRET': 'TURNSTILE_SECRET',
    'TURNSTILE_SITE_KEY': 'TURNSTILE_SITE_KEY',
}
PROJECTS_ENV = Path.home() / 'Documents' / 'projects' / '.env'


def checkout_envs(root: Path) -> list[Path]:
    """This checkout's .env, then the main checkout's when this is a git worktree. The .env is ignored by git, so a
    worktree has none, and without this one it fell through to the org key, which Claude refuses without a workspace
    id (metamorphic run, 6 Nov)."""
    out = [root / '.env']
    git = root / '.git'
    if git.is_file():  # a worktree: ".git" holds "gitdir: <main>/.git/worktrees/<name>"
        gitdir = Path(git.read_text().partition('gitdir:')[2].strip())
        if gitdir.parent.name == 'worktrees':
            out.append(gitdir.parent.parent.parent / '.env')
    return out


def load() -> None:
    files = [*checkout_envs(Path(__file__).resolve().parents[2]), PROJECTS_ENV]
    for f in files:
        if not f.is_file():
            continue
        for line in f.read_text().splitlines():
            name, sep, value = line.strip().partition('=')
            if not sep or name.startswith('#'):
                continue
            value = value.strip().strip('"').strip("'")
            target = NAMES.get(name, name if name.isupper() else None)
            if target and value and not os.environ.get(target):
                os.environ[target] = value


if __name__ == '__main__':
    load()
    for target in sorted(set(NAMES.values())):
        print(f'{target:24} {"set" if os.environ.get(target) else "missing"}')
