"""The brain: a fallback chain of LLMs from screen.yaml. Links without a key are skipped.

Claude runs with thinking off (`between_tools`): on a phone call the wait before the first word is what the caller \
hears.
"""
import os

import anthropic
import httpx2 as httpx
from livekit.agents import llm
from livekit.plugins import anthropic as lk_anthropic
from livekit.plugins import google as lk_google
from livekit.plugins import openai as lk_openai

INFERHUB_URL = 'https://octodb.design-bakery.com/api/vault/proxy/https://api.inferhub.dev/v1'
# Thinking off per model: Sonnet 5.5 takes between_tools; Haiku 5.5 takes disabled (at effort high or below).
THINKING_OFF = {'claude-sonnet-5-5': {'type': 'between_tools'}, 'claude-haiku-5-5': {'type': 'disabled'}}


def anthropic_client(read_timeout: float = 15.0) -> anthropic.AsyncClient:
    """The Anthropic client every part of Riley uses: the workspace header our key needs, and no retries (the
    chain, or the caller, decides what happens next). Intake drafts a job post with it too."""
    headers = {}
    if os.environ.get('ANTHROPIC_WORKSPACE_ID', '').startswith('wrkspc_'):
        headers['anthropic-workspace-id'] = os.environ['ANTHROPIC_WORKSPACE_ID']
    return anthropic.AsyncClient(
        api_key=os.environ['ANTHROPIC_API_KEY'], max_retries=0, default_headers=headers,
        http_client=httpx.AsyncClient(timeout=httpx.Timeout(5.0, read=read_timeout), follow_redirects=True))


class Claude(lk_anthropic.LLM):
    """The LiveKit plugin with the request fields it has no option for: thinking off and the workspace header."""

    def __init__(self, model: str):
        super().__init__(model=model, client=anthropic_client(), caching='ephemeral', max_tokens=400)
        self._thinking = THINKING_OFF.get(model)

    def chat(self, **kw):
        extra = kw.pop('extra_kwargs', None)
        extra = dict(extra) if isinstance(extra, dict) else {}
        # current Claude models reject sampling settings (the test judge sends one)
        for k in ('temperature', 'top_p', 'top_k'):
            extra.pop(k, None)
        if self._thinking:
            extra['extra_body'] = {**(extra.get('extra_body') or {}), 'thinking': self._thinking}
        return super().chat(**kw, extra_kwargs=extra)


def link(spec: dict) -> llm.LLM | None:
    p, model = spec['provider'], spec['model']
    if p == 'anthropic' and os.environ.get('ANTHROPIC_API_KEY'):
        return Claude(model)
    if p == 'google' and os.environ.get('GOOGLE_API_KEY'):
        return lk_google.LLM(model=model)
    if p == 'inferhub' and os.environ.get('INFERHUB_API_KEY'):
        return lk_openai.LLM(model=model, base_url=os.environ.get('INFERHUB_BASE_URL', INFERHUB_URL),
                             api_key=os.environ['INFERHUB_API_KEY'])
    return None


def build(stack: dict) -> tuple[llm.LLM, list[str]]:
    """The chain and the names of the links in it, first choice first."""
    links = [(f"{s['provider']}:{s['model']}", m) for s in stack['brain'] if (m := link(s))]
    if not links:
        raise RuntimeError('no brain has a key: set ANTHROPIC_API_KEY (and ANTHROPIC_WORKSPACE_ID) or another link')
    names = [n for n, _ in links]
    if len(links) == 1:
        return links[0][1], names
    return llm.FallbackAdapter([m for _, m in links], attempt_timeout=stack.get('brain_timeout_s', 4)), names
