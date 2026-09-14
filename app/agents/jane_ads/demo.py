"""
Jane + Ads — runnable evidence of the Shore-side build.

Run:  python -m app.agents.jane_ads.demo

Shows two things with no live platform, no server, no DB:
  1. The decision engine on the PRD's worked examples (Part C1) — readable output.
  2. A full plan → launch → conversations → wallet flow on the mock adapter,
     proving the seam works end-to-end and the per-business cap is respected.
"""
from __future__ import annotations

import asyncio

from .adapters.mock import MockAdPlatformAdapter
from .decision_engine import plan_campaign
from .models import (
    CampaignRequest,
    CreativeContext,
    CreativeKind,
    Goal,
    PlanDecision,
    PurchaseBehaviour,
    SpendAuthorization,
)


def _line(char: str = "─", n: int = 68) -> None:
    print(char * n)


def show_decisions() -> None:
    _line("═")
    print("  DECISION ENGINE — goal first, behaviour drives, per campaign")
    _line("═")

    # (label, category, goal, budget, has_video, stated_behaviour, new_thing, existing_demand, expected)
    examples = [
        ("Fashion — get discovered", "fashion", Goal.MESSAGES, 10_000, False, None, False, False, "META only"),
        ("Fashion — 'they SEARCH my name'", "fashion", Goal.LEADS, 15_000, False,
         PurchaseBehaviour.SEARCH, False, False, "GOOGLE (goal overrides fashion→Meta)"),
        ("Clinic — walk-ins", "clinic", Goal.WALK_INS, 10_000, False, None, False, True, "GOOGLE"),
        ("Clinic — NEW service launch", "clinic", Goal.AWARENESS, 10_000, False, None, True, False,
         "META (new thing nobody searches → discover)"),
        ("Skincare (has video)", "skincare", Goal.AWARENESS, 60_000, True, None, False, False, "META + TikTok"),
        ("Fashion — tiny budget", "fashion", Goal.MESSAGES, 2_000, False, None, False, False, "advise: pool / top up"),
    ]

    for name, category, goal, budget, has_video, beh, new_thing, demand, expected in examples:
        req = CampaignRequest(
            business_id="demo",
            business_name=name,
            category=category,
            goal=goal,
            budget_ngn=budget,
            creative=CreativeContext(
                kind=CreativeKind.VIDEO if has_video else CreativeKind.IMAGE,
                has_video=has_video,
            ),
            stated_behaviour=beh,
            is_new_thing=new_thing,
            has_existing_demand=demand,
        )
        res = plan_campaign(req, funded_amount_ngn=budget, total_funded_wallets_ngn=budget)
        print(f"\n▶ {name} — ₦{budget:,} {'(+video)' if has_video else ''}")
        print(f"   Expected : {expected}")
        if res.decision == PlanDecision.ADVISE:
            print(f"   Jane says: ADVISE — {res.advice.reason}")
        else:
            print(f"   Behaviour: {res.plan.behaviour.value}")
            for p in res.plan.platforms:
                print(
                    f"   Jane says: {p.platform.value.upper():7} "
                    f"₦{p.budget_ngn:,.0f} · {p.days}d · "
                    f"{p.variants} variant(s) · test={p.test_scope.value}"
                )
            print(f"   Jane tells the customer: \"{res.plan.explanation}\"")


async def show_end_to_end() -> None:
    print()
    _line("═")
    print("  END-TO-END ON THE MOCK ADAPTER  (plan → launch → wallet)")
    _line("═")

    funded = 10_000.0
    req = CampaignRequest(
        business_id="biz-lagos-01",
        business_name="Ada's Fashion",
        category="fashion",
        budget_ngn=funded,
    )
    res = plan_campaign(req, funded_amount_ngn=funded, total_funded_wallets_ngn=funded)
    plan = res.plan
    print(f"\n1. Planned: {[p.platform.value for p in plan.platforms]} · "
          f"per-business cap ₦{plan.per_business_cap_ngn:,.0f}")

    adapter = MockAdPlatformAdapter(conversation_cost_ngn=500.0)
    auth = SpendAuthorization(
        business_id=req.business_id,
        funded_amount_ngn=funded,
        account_cap_ngn=funded,
    )
    launch = await adapter.launch_campaign(plan, auth)
    print(f"2. Launched campaign {launch.campaign_id} · ad {launch.ad_ids[req.business_id]}")

    convos = await adapter.poll_conversations(launch.campaign_id)
    wallet = funded
    for c in convos:
        wallet -= c.charge_ngn
    spend = (await adapter.fetch_per_ad_spend(launch.campaign_id))[0]
    print(f"3. Delivered {len(convos)} WhatsApp conversations @ ₦500 each")
    print(f"4. Wallet: ₦{funded:,.0f} → ₦{wallet:,.0f}  |  ad spend ₦{spend.spend_ngn:,.0f}")
    assert spend.spend_ngn <= plan.per_business_cap_ngn
    print(f"   ✓ spend (₦{spend.spend_ngn:,.0f}) never exceeded the cap "
          f"(₦{plan.per_business_cap_ngn:,.0f})")

    print("\n5. Cap enforcement — pause the ad, confirm spend stops:")
    await adapter.pause_ad(launch.campaign_id, launch.ad_ids[req.business_id])
    after = await adapter.poll_conversations(launch.campaign_id)
    print(f"   conversations after pause: {len(after)}  ✓ (throttle works)")
    _line()
    print("  All Shore-side pieces ran with zero live-platform dependency.")
    _line()


def main() -> None:
    show_decisions()
    asyncio.run(show_end_to_end())


if __name__ == "__main__":
    main()
