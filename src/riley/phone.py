"""Phone setup on LiveKit Cloud: one dispatch rule that sends calls to Riley's number to the nurse-screen agent.

Run: .venv/bin/python -m agent.phone +12125550100   (creates the rule once for that number; safe to rerun)
     .venv/bin/python -m agent.phone --list          (shows rules and inbound trunks)
Buy the number in the LiveKit Cloud dashboard (Telephony > Phone Numbers). The rule only takes calls to that number,
so other agents in the same project keep their own calls.
"""
import asyncio
import sys

from livekit import api

from riley import keys

RULE = 'nurse-screen'


async def main() -> None:
    keys.load()
    lk = api.LiveKitAPI()  # reads LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET
    try:
        rules = (await lk.sip.list_dispatch_rule(api.ListSIPDispatchRuleRequest())).items
        if '--list' in sys.argv:
            trunks = (await lk.sip.list_inbound_trunk(api.ListSIPInboundTrunkRequest())).items
            for t in trunks:
                print(f'trunk {t.sip_trunk_id} {t.name} numbers={list(t.numbers)}')
            for r in rules:
                print(f'rule  {r.sip_dispatch_rule_id} {r.name} agents={[a.agent_name for a in r.room_config.agents]}')
            return
        number = next((a for a in sys.argv[1:] if a.startswith('+')), None)
        if not number:
            sys.exit('give the number in E.164 form, e.g. +12125550100')
        if any(r.name == RULE for r in rules):
            print(f'dispatch rule {RULE} already exists')
            return
        rule = await lk.sip.create_dispatch_rule(api.CreateSIPDispatchRuleRequest(dispatch_rule=api.SIPDispatchRuleInfo(
            name=RULE,
            numbers=[number],  # the dialed number; inbound_numbers would filter on the caller instead
            # each caller gets a fresh room, so calls never share a session
            rule=api.SIPDispatchRule(dispatch_rule_individual=api.SIPDispatchRuleIndividual(room_prefix='screen-')),
            room_config=api.RoomConfiguration(agents=[api.RoomAgentDispatch(agent_name='nurse-screen')]),
        )))
        print(f'created dispatch rule {rule.sip_dispatch_rule_id}')
    finally:
        await lk.aclose()


if __name__ == '__main__':
    asyncio.run(main())
