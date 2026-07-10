"""
Jane + Ads — demo router.

Two endpoints, no auth (internal evidence UI):
  POST /jane-ads/plan   — run the real decision engine + a mock end-to-end
  GET  /jane-ads/demo   — a self-contained HTML page to click through it

The HTML page is served from the backend so it calls /jane-ads/plan same-origin
(no CORS). It uses the ACTUAL decision engine — nothing is duplicated in JS.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from app.core.auth_bearer import JWTBearer
from app.dependencies import get_db_dependency

from .adapters.mock import MockAdPlatformAdapter
from .decision_engine import plan_campaign
from .models import (
    CampaignRequest,
    CreativeContext,
    CreativeKind,
    Goal,
    PlanDecision,
    PurchaseBehaviour,
)
from .payments import JaneAdsPayments
from .store import InMemoryWalletStore, MongoWalletStore
from .wallet import InsufficientFundsError, MinimumTopUpError, WalletService

router = APIRouter(prefix="/jane-ads", tags=["Jane + Ads (demo)"])


class PlanRequestBody(BaseModel):
    business_name: str = "My Business"
    category: str = ""
    description: str = ""
    goal: Goal = Goal.MESSAGES
    budget_ngn: float = Field(10_000, gt=0)
    has_video: bool = False
    stated_behaviour: Optional[PurchaseBehaviour] = None
    is_new_thing: bool = False
    has_existing_demand: bool = False
    geo: str = ""
    city: str = ""                    # e.g. "Surulere" — enables pin-and-pocket geo
    conversation_cost_ngn: float = Field(500.0, gt=0)


@router.post("/plan")
async def plan(body: PlanRequestBody) -> dict:
    """Run the decision engine, then (if it produced a plan) a mock end-to-end so the
    UI can show conversations delivered + wallet movement + cap enforcement."""
    req = CampaignRequest(
        business_id="demo",
        business_name=body.business_name,
        category=body.category,
        description=body.description,
        goal=body.goal,
        budget_ngn=body.budget_ngn,
        creative=CreativeContext(
            kind=CreativeKind.VIDEO if body.has_video else CreativeKind.IMAGE,
            has_video=body.has_video,
        ),
        stated_behaviour=body.stated_behaviour,
        is_new_thing=body.is_new_thing,
        has_existing_demand=body.has_existing_demand,
        geo=body.geo,
    )
    result = plan_campaign(req, funded_amount_ngn=body.budget_ngn,
                           total_funded_wallets_ngn=body.budget_ngn)

    if result.decision == PlanDecision.ADVISE:
        return {
            "decision": "advise",
            "advice": result.advice.model_dump(),
            "trace": result.advice.trace,
        }

    plan_obj = result.plan

    # Geo refinement — pin-and-pocket targeting within the chosen platform.
    geo_dump = None
    if body.city:
        from .geo import geo_for_request
        geo_plan = await geo_for_request(
            body.business_name, body.category, body.city, body.goal, body.description
        )
        plan_obj.geo = geo_plan
        geo_dump = geo_plan.model_dump()

    # End-to-end on the REAL wallet + mock adapter: fund the wallet, launch, then charge
    # each delivered conversation through the actual WalletService — so the KPIs show
    # genuine prepaid-first + dynamic pricing, not ad-hoc math.
    wallet = WalletService(InMemoryWalletStore())
    await wallet.top_up(req.business_id, body.budget_ngn, reference="demo-topup")
    adapter = MockAdPlatformAdapter(conversation_cost_ngn=body.conversation_cost_ngn)
    auth = await wallet.authorization_for(req.business_id, body.budget_ngn)
    launch = await adapter.launch_campaign(plan_obj, auth)
    delivered = await adapter.poll_conversations(launch.campaign_id)

    charged = 0
    prices: list[float] = []
    for conv in delivered:
        try:
            txn = await wallet.charge_conversation(
                req.business_id, campaign_id=launch.campaign_id, ad_id=conv.ad_id,
                actual_platform_cost_ngn=body.conversation_cost_ngn,
            )
            charged += 1
            prices.append(-txn.amount_ngn)
        except InsufficientFundsError:
            break   # prepaid-first — nothing runs once the wallet is empty

    balance_after = await wallet.get_balance(req.business_id)
    spent = round(body.budget_ngn - balance_after, 2)

    return {
        "decision": "plan",
        "goal": plan_obj.goal.value,
        "behaviour": plan_obj.behaviour.value,
        "explanation": plan_obj.explanation,
        "trace": plan_obj.trace,
        "per_business_cap_ngn": plan_obj.per_business_cap_ngn,
        "account_cap_ngn": plan_obj.account_cap_ngn,
        "geo": geo_dump,
        "platforms": [p.model_dump() for p in plan_obj.platforms],
        "simulation": {
            "conversations_delivered": len(delivered),
            "conversations_charged": charged,
            "prepaid_stopped": charged < len(delivered),
            "price_min_ngn": min(prices) if prices else 0,
            "price_max_ngn": max(prices) if prices else 0,
            "wallet_before_ngn": body.budget_ngn,
            "wallet_after_ngn": balance_after,
            "spent_ngn": spent,
            "cap_respected": spent <= plan_obj.per_business_cap_ngn,
        },
    }


# ── Real wallet funding via Squad ─────────────────────────────────────────────

class TopUpBody(BaseModel):
    business_id: str
    amount_ngn: float = Field(..., gt=0)
    email: str


@router.post("/wallet/topup")
async def wallet_topup(
    body: TopUpBody,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    _token: dict = Depends(JWTBearer()),
) -> dict:
    """Start a real Squad checkout to fund a business's ad wallet. Returns the
    checkout URL the customer opens to pay. Nothing is credited until Squad confirms."""
    try:
        result = await JaneAdsPayments(db).initialize_topup(
            body.business_id, body.amount_ngn, body.email
        )
    except MinimumTopUpError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not start payment: {e}")
    return {"status": "checkout_created", **result}


@router.get("/wallet/topup/{reference}/verify")
async def wallet_topup_verify(
    reference: str,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    _token: dict = Depends(JWTBearer()),
) -> dict:
    """Verify a top-up with Squad and credit the wallet if it succeeded (idempotent)."""
    return await JaneAdsPayments(db).confirm_topup(reference)


@router.post("/wallet/webhook")
async def wallet_webhook(
    request: Request,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
) -> dict:
    """Squad → us. Credits the wallet on a successful top-up (idempotent). No JWT —
    Squad calls this directly; only references we created are acted on."""
    payload = await request.json()
    return await JaneAdsPayments(db).handle_webhook(payload)


@router.get("/wallet/{business_id}/balance")
async def wallet_balance(
    business_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    _token: dict = Depends(JWTBearer()),
) -> dict:
    """Current balance + recent ledger entries for a business's ad wallet."""
    wallet = WalletService(MongoWalletStore(db))
    balance = await wallet.get_balance(business_id)
    txns = await wallet.list_transactions(business_id)
    return {
        "business_id": business_id,
        "balance_ngn": balance,
        "transactions": [t.model_dump(mode="json") for t in txns[-20:]],
    }


@router.get("/demo", response_class=HTMLResponse)
async def demo_page() -> str:
    return _DEMO_HTML


_DEMO_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Jane + Ads — Decision Engine</title>
<style>
  :root { --pink:#C2185B; --ink:#111; --muted:#888; --bg:#faf8f7; }
  * { box-sizing:border-box; }
  body { font-family:-apple-system,Segoe UI,Roboto,sans-serif; margin:0; background:var(--bg); color:var(--ink); }
  .wrap { max-width:720px; margin:0 auto; padding:32px 20px 60px; }
  h1 { font-size:22px; margin:0 0 4px; }
  .sub { color:var(--muted); font-size:14px; margin:0 0 24px; }
  .card { background:#fff; border:1px solid #eee; border-radius:14px; padding:20px; margin-bottom:16px; }
  label { display:block; font-size:12px; font-weight:700; color:#555; margin:12px 0 4px; text-transform:uppercase; letter-spacing:.4px; }
  input[type=text], input[type=number], select { width:100%; padding:10px 12px; border:1.5px solid #e0dcd9; border-radius:9px; font-size:14px; }
  .row { display:flex; gap:12px; } .row > div { flex:1; }
  .chk { display:flex; align-items:center; gap:8px; margin-top:14px; font-size:14px; }
  button { margin-top:18px; width:100%; padding:13px; border:none; border-radius:10px;
    background:linear-gradient(135deg,#C2185B,#8E1545); color:#fff; font-weight:800; font-size:15px; cursor:pointer; }
  .examples { display:flex; flex-wrap:wrap; gap:8px; margin-top:10px; }
  .ex { font-size:12px; padding:5px 10px; border:1px solid #e0dcd9; border-radius:20px; background:#fff; cursor:pointer; }
  .out { display:none; }
  .verdict { font-size:18px; font-weight:800; margin:0 0 6px; }
  .why { color:#444; font-style:italic; margin:0 0 16px; }
  .plat { display:flex; justify-content:space-between; align-items:center; padding:12px 14px; border:1.5px solid #C2185B33; background:#fff8fb; border-radius:10px; margin-bottom:8px; }
  .plat b { font-size:15px; } .plat .meta { color:var(--muted); font-size:12px; }
  .sim { display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-top:8px; }
  .kpi { background:#f6f5f3; border-radius:10px; padding:12px; }
  .kpi .n { font-size:20px; font-weight:800; } .kpi .l { font-size:11px; color:var(--muted); text-transform:uppercase; }
  .ok { color:#16a34a; font-weight:700; } .advise { color:var(--pink); font-weight:700; }
  .pill { display:inline-block; font-size:11px; font-weight:700; padding:3px 9px; border-radius:20px; background:#eee; color:#555; text-transform:uppercase; }
  .thinking { font-size:13px; font-weight:700; color:var(--pink); margin:0 0 12px; }
  .steps { list-style:none; padding:0; margin:0 0 18px; counter-reset:s; }
  .steps li { position:relative; padding:9px 12px 9px 40px; margin-bottom:7px; background:#f6f5f3;
    border-left:3px solid var(--pink); border-radius:6px; font-size:13px; color:#333;
    opacity:0; transform:translateY(6px); transition:opacity .3s, transform .3s; }
  .steps li.show { opacity:1; transform:none; }
  .steps li::before { counter-increment:s; content:counter(s); position:absolute; left:10px; top:9px;
    width:20px; height:20px; border-radius:50%; background:var(--pink); color:#fff;
    font-size:11px; font-weight:800; display:flex; align-items:center; justify-content:center; }
  .divider { border:0; border-top:1px dashed #ddd; margin:16px 0; }
  /* Decision-tree diagram */
  .tree-wrap { margin:0 0 16px; }
  .tree-wrap > summary { cursor:pointer; font-weight:800; font-size:14px; color:var(--pink);
    padding:14px 16px; background:#fff; border:1px solid #eee; border-radius:12px; list-style:none; }
  .tree-wrap > summary::-webkit-details-marker { display:none; }
  .tree-wrap[open] > summary { border-radius:12px 12px 0 0; border-bottom:none; }
  .tree { background:#fff; border:1px solid #eee; border-top:none; border-radius:0 0 12px 12px; padding:6px 16px 18px; }
  .lane { border:1.5px dashed var(--pink); border-radius:10px; padding:11px 14px; background:#fff8fb; }
  .lane .tag { display:block; font-size:10px; font-weight:800; letter-spacing:.6px; color:var(--pink); text-transform:uppercase; margin-bottom:3px; }
  .lane .quote { font-style:italic; color:#444; font-size:13px; margin:0; }
  .lane .inputs { display:flex; flex-wrap:wrap; gap:6px; margin-top:8px; }
  .chip { font-size:11px; font-weight:700; padding:3px 9px; border-radius:20px; background:#f0eded; color:#555; }
  .flow { text-align:center; color:var(--muted); font-size:16px; line-height:1; margin:5px 0; }
  .flow small { display:block; font-size:10px; letter-spacing:.4px; text-transform:uppercase; margin-top:2px; }
  .rail { border-top:2px solid var(--pink); text-align:center; margin:12px 0 10px; }
  .rail span { position:relative; top:-9px; background:#fff; padding:0 10px; font-size:10px; font-weight:800;
    letter-spacing:1px; text-transform:uppercase; color:var(--pink); }
  .node { display:flex; gap:11px; padding:10px 12px; border-radius:9px; background:#f6f5f3; margin-bottom:7px; }
  .node .num { flex-shrink:0; width:22px; height:22px; border-radius:50%; background:var(--pink); color:#fff;
    font-size:11px; font-weight:800; display:flex; align-items:center; justify-content:center; }
  .node .body { font-size:13px; color:#222; } .node .body b { color:#111; }
  .branchset { display:flex; flex-wrap:wrap; gap:6px; margin-top:7px; }
  .branch { font-size:11px; font-weight:700; padding:3px 9px; border-radius:6px; border:1px solid #C2185B33;
    background:#fff8fb; color:#333; }
  .branch b { color:var(--pink); }
</style></head>
<body><div class="wrap">
  <h1>Jane + Ads — Decision Engine</h1>
  <p class="sub">Goal first, behaviour next, business type is only a hint — decided per campaign, always explained. Pick a scenario or fill it in; Jane reasons it out live from the real engine.</p>

  <details class="tree-wrap">
    <summary>▸ How Jane decides — the logic</summary>
    <div class="tree">
      <div class="lane">
        <span class="tag">Layer 0 · the LLM (Jane) understands</span>
        <p class="quote">"I want people who already know my boutique to find me — they can't reach me on Google."</p>
        <div class="inputs">
          <span class="chip">goal: leads</span>
          <span class="chip">behaviour: search</span>
          <span class="chip">budget: ₦15,000</span>
          <span class="chip">creative: photos</span>
          <span class="chip">geo: Lekki</span>
        </div>
      </div>
      <div class="flow">↓<small>hands structured inputs to the rule engine</small></div>
      <div class="rail"><span>Deterministic decision tree</span></div>

      <div class="node"><div class="num">1</div><div class="body"><b>Goal leads.</b> The goal of THIS campaign drives everything — decided per campaign, never per business.</div></div>
      <div class="node"><div class="num">2</div><div class="body"><b>Behaviour.</b> Business type sets a default; the user's stated behaviour or the goal overrides it.
        <div class="branchset"><span class="branch">default (hint)</span><span class="branch">→ user override</span><span class="branch">→ goal implication</span></div></div></div>
      <div class="node"><div class="num">3</div><div class="body"><b>Behaviour → platforms.</b>
        <div class="branchset"><span class="branch"><b>search</b> → Google</span><span class="branch"><b>discover</b> → Meta / TikTok</span><span class="branch"><b>mixed</b> → Meta + Google</span></div></div></div>
      <div class="node"><div class="num">4</div><div class="body"><b>Creative gate.</b> No native video → TikTok removed. Google Search needs no creative.</div></div>
      <div class="node"><div class="num">5</div><div class="body"><b>Budget gate.</b>
        <div class="branchset"><span class="branch">below floor → <b>advise</b> (pool / top up)</span><span class="branch">small → <b>one</b> best fit</span><span class="branch">funds several → <b>run several</b></span></div></div></div>
      <div class="node"><div class="num">6</div><div class="body"><b>Geography.</b> Radius / city / pin — a targeting setting WITHIN the platform, not a reason to switch platforms.</div></div>
      <div class="node"><div class="num">7</div><div class="body"><b>Recommend + explain.</b> Name the platform(s) AND explain why, in plain language — always. Both caps (per-business + per-account) attached.</div></div>
    </div>
  </details>

  <div class="card">
    <div class="row">
      <div><label>Business name</label><input type="text" id="name" value="Ada's Closet"/></div>
      <div><label>Category (hint only)</label><input type="text" id="cat" value="fashion"/></div>
    </div>
    <div class="row">
      <div><label>Goal of this campaign</label>
        <select id="goal">
          <option value="messages">Messages (WhatsApp)</option>
          <option value="leads">Leads</option>
          <option value="bookings">Bookings</option>
          <option value="walk_ins">Walk-ins</option>
          <option value="awareness">Awareness</option>
          <option value="sales">Sales</option>
        </select></div>
      <div><label>Budget (₦)</label><input type="number" id="budget" value="10000"/></div>
    </div>
    <label>How do customers buy this? (override the hint)</label>
    <select id="beh">
      <option value="">— use the business-type default —</option>
      <option value="search">They SEARCH for it (Google)</option>
      <option value="discover">They DISCOVER it scrolling (Meta/TikTok)</option>
      <option value="mixed">Both</option>
    </select>
    <label>City / area — enables pin-and-pocket targeting (optional)</label>
    <input type="text" id="city" placeholder="e.g. Surulere, Lagos, Lekki"/>
    <label class="chk"><input type="checkbox" id="video"/> Has native video (enables TikTok)</label>
    <label class="chk"><input type="checkbox" id="newthing"/> Brand-new thing nobody searches for yet</label>
    <label class="chk"><input type="checkbox" id="demand"/> People already look for this</label>
    <div class="examples">
      <span class="ex" onclick="ex({name:'Mama Kitchen',cat:'restaurant',goal:'messages',budget:10000,city:'Surulere'})">Lunch spot · Surulere pins</span>
      <span class="ex" onclick="ex({name:'Prime Homes',cat:'luxury real estate',goal:'leads',budget:60000,city:'Lagos'})">Luxury realtor · wealth pockets</span>
      <span class="ex" onclick="ex({name:'Ada Closet',cat:'fashion',goal:'leads',budget:15000,beh:'search'})">Fashion · they SEARCH my name</span>
      <span class="ex" onclick="ex({name:'Okafor Clinic',cat:'clinic',goal:'awareness',budget:10000,newthing:true})">Clinic · new-service launch</span>
      <span class="ex" onclick="ex({name:'GlowUp',cat:'skincare',goal:'awareness',budget:60000,video:true,city:'Lekki'})">Skincare ₦60k +video</span>
      <span class="ex" onclick="ex({name:'Tiny Shop',cat:'fashion',goal:'messages',budget:2000})">Tiny ₦2k</span>
    </div>
    <button onclick="run()">Ask Jane</button>
  </div>

  <div class="card out" id="out"></div>
</div>
<script>
function ex(o){
  document.getElementById('name').value=o.name||'';
  document.getElementById('cat').value=o.cat||'';
  document.getElementById('goal').value=o.goal||'messages';
  document.getElementById('budget').value=o.budget||10000;
  document.getElementById('beh').value=o.beh||'';
  document.getElementById('city').value=o.city||'';
  document.getElementById('video').checked=!!o.video;
  document.getElementById('newthing').checked=!!o.newthing;
  document.getElementById('demand').checked=!!o.demand;
  run();
}
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
async function run(){
  const beh=document.getElementById('beh').value;
  const body={business_name:document.getElementById('name').value,category:document.getElementById('cat').value,
    goal:document.getElementById('goal').value,
    budget_ngn:parseFloat(document.getElementById('budget').value||'0'),
    has_video:document.getElementById('video').checked,
    is_new_thing:document.getElementById('newthing').checked,
    has_existing_demand:document.getElementById('demand').checked,
    city:document.getElementById('city').value,
    stated_behaviour:beh||null};
  const out=document.getElementById('out');out.style.display='block';
  const naira=n=>'₦'+Number(n).toLocaleString();
  const esc=t=>String(t).replace(/</g,'&lt;');
  // 1. Reveal Jane's reasoning steps one at a time.
  out.innerHTML='<p class="thinking">🧠 Jane is working it out…</p><ul class="steps" id="steps"></ul>';
  let d;
  try{
    const r=await fetch('/jane-ads/plan',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    if(!r.ok) throw new Error('HTTP '+r.status);
    d=await r.json();
    if(!d || !d.decision) throw new Error('unexpected response');
  }catch(err){
    out.innerHTML='<p class="verdict advise">Couldn\\'t reach Jane</p>'+
      '<p class="why">The server was busy reloading for a second. Just click Ask Jane again.</p>';
    return;
  }
  const ul=document.getElementById('steps');
  for(const step of (d.trace||[])){
    const li=document.createElement('li');li.innerHTML=esc(step);ul.appendChild(li);
    await sleep(60);li.classList.add('show');await sleep(480);
  }
  document.querySelector('.thinking').textContent='🧠 How Jane decided';
  await sleep(250);
  // 2. Then reveal the verdict below the reasoning.
  if(d.decision==='advise'){
    out.insertAdjacentHTML('beforeend','<hr class="divider"/>'+
      '<p class="verdict advise">Jane advises: don\\'t run yet</p>'+
      '<p class="why">'+d.advice.reason+'</p>'+
      (d.advice.can_pool?'<p class="ok">✓ Can pool with similar businesses to clear the floor.</p>':''));
    return;
  }
  let html='<hr class="divider"/>'+
    '<p class="verdict">'+d.platforms.map(p=>p.platform.toUpperCase()).join(' + ')+'</p>'+
    '<span class="pill">goal: '+d.goal+'</span> <span class="pill">'+d.behaviour+'</span> '+
    '<span class="pill">cap '+naira(d.per_business_cap_ngn)+'</span>'+
    '<p class="why">"'+d.explanation+'"</p>';
  d.platforms.forEach(p=>{html+='<div class="plat"><b>'+p.platform.toUpperCase()+'</b>'+
    '<span class="meta">'+naira(p.budget_ngn)+' · '+p.days+' days · '+p.variants+' variant(s) · test: '+p.test_scope+'</span></div>';});
  if(d.geo){
    const g=d.geo;
    html+='<p class="thinking" style="margin-top:18px">📍 Geo — '+(g.mode==='watering_hole'?'watering-hole (go to where they gather)':'own-radius (pull them in)')+'</p>';
    if(g.pins && g.pins.length){
      g.pins.forEach(pin=>{html+='<div class="plat"><b>'+esc(pin.name)+'</b>'+
        '<span class="meta">~'+pin.radius_km+'km · '+esc(pin.reason||'')+'</span></div>';});
      html+='<p class="why">"'+esc(g.explanation)+'"</p>';
    } else {
      html+='<p class="why">"'+esc(g.explanation)+'"</p>';
    }
  }
  const s=d.simulation;
  const priceLabel = s.price_min_ngn===s.price_max_ngn
    ? naira(s.price_max_ngn)
    : naira(s.price_min_ngn)+'→'+naira(s.price_max_ngn)+' (dynamic)';
  const convLabel = s.prepaid_stopped
    ? s.conversations_charged+' of '+s.conversations_delivered+' (prepaid cap hit)'
    : s.conversations_charged;
  html+='<p class="thinking" style="margin-top:18px">💳 Real wallet — top up, charge, prepaid-first</p>'+
    '<div class="sim">'+
    '<div class="kpi"><div class="n">'+convLabel+'</div><div class="l">Conversations charged</div></div>'+
    '<div class="kpi"><div class="n">'+priceLabel+'</div><div class="l">Price / conversation</div></div>'+
    '<div class="kpi"><div class="n">'+naira(s.wallet_before_ngn)+' → '+naira(s.wallet_after_ngn)+'</div><div class="l">Wallet balance</div></div>'+
    '<div class="kpi"><div class="n '+(s.cap_respected?'ok':'')+'">'+(s.cap_respected?'✓ within cap':'✗ over cap')+'</div><div class="l">Spend ('+naira(s.spent_ngn)+')</div></div>'+
    '</div>';
  out.insertAdjacentHTML('beforeend',html);
}
</script>
</body></html>"""
