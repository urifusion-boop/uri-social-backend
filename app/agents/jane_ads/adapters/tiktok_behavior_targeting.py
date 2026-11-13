"""
TikTok audience targeting by what people DO, not only what they are into.

Ads Manager's Audience > Interests & behaviors box has more than "Interests": Purchase intention, Video
interactions, Creator interactions and Hashtag interactions (creator interactions are not sent, see below). The ad group only ever set interest_category_ids, so a
Jane ad reached everyone interested in the topic and ignored the people who had just watched, followed or searched it.

Field names and limits below are from TikTok's own Marketing API SDK (adgroup/create):
  * actions[]  {action_scene, action_period, video_user_actions, action_category_ids}. action_scene is VIDEO_RELATED
    (watched / liked / commented / shared, over 7 or 15 days), CREATOR_RELATED (follows / visits a creator's page) or
    HASHTAG_RELATED (viewed a hashtag). Valid only when TikTok is the only placement, which is how Jane launches.
    Video AND creator ids both come from GET /tool/action_category/, one flat list whose entries each carry an
    action_scene (CREATOR_RELATED / VIDEO_RELATED) and have separate ids, so each scene must be given only its own
    (staging 2026-11-11: creator scene rejected when handed video ids). Creator actions are FOLLOWING / VIEW_HOMEPAGE,
    not the video ones. Hashtag ids come from GET /tool/hashtag/recommend/.
  * purchase_intention_keyword_ids, from GET /tool/interest_keyword/recommend/ with audience_type=PURCHASE_INTENTION.
    The SDK says TikTok accepts it only for the App promotion, Conversions and Product sales objectives, but the
    Ads Manager offers it on Jane's Traffic campaigns, so it is tried on every launch and dropped (see
    BehaviorTargeting.attempts) if TikTok rejects it.

None of it is allowed to cost a launch. Every lookup that fails or finds nothing just leaves its audience out, and the
caller retries the ad group without these fields if TikTok rejects them. The RESPONSE shapes of the lookups are not in
the SDK, so they are read loosely (any {id, name} entries, however nested).
"""
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

import httpx

from app.core.config import settings

VIDEO_ACTIONS = ["WATCHED_TO_END", "LIKED", "COMMENTED", "SHARED"]
CREATOR_ACTIONS = ["FOLLOWING", "VIEW_HOMEPAGE"]
HASHTAG_ACTIONS = ["VIEW_HASHTAG"]
VIDEO_ACTION_DAYS = 15            # the longer of TikTok's 7 / 15 day windows: a wider, not a tighter, audience
MAX_ACTION_CATEGORIES = 3         # per scene; Jane's interests are already few, and each one narrows the audience
MAX_HASHTAGS = 5
MAX_PURCHASE_INTENT = 5



@dataclass
class BehaviorTargeting:
    actions: list = field(default_factory=list)
    purchase_intention_keyword_ids: list = field(default_factory=list)

    def fields(self) -> dict:
        """Every extra ad group field there is (empty when there are none)."""
        out: dict = {}
        if self.actions:
            out["actions"] = self.actions
        if self.purchase_intention_keyword_ids:
            out["purchase_intention_keyword_ids"] = self.purchase_intention_keyword_ids
        return out

    def attempts(self) -> list:
        """The field sets to try for the ad group, widest first, ending with none: everything, then everything but one
        audience (purchase intention first, the one TikTok is likeliest to refuse), then one interaction at a time, then
        purchase intention alone. A rejected create leaves nothing behind, so an audience TikTok does not accept costs
        only a few retries, not the launch and not the audiences that are accepted."""
        out: list = []

        def add(d: dict) -> None:
            if d and d not in out:
                out.append(d)

        def build(actions: list, purchase: list) -> dict:
            return {**({"actions": actions} if actions else {}),
                    **({"purchase_intention_keyword_ids": purchase} if purchase else {})}

        purchase = self.purchase_intention_keyword_ids
        add(build(self.actions, purchase))
        add(build(self.actions, []))
        for i in reversed(range(len(self.actions))):
            add(build(self.actions[:i] + self.actions[i + 1:], purchase))
        for action in self.actions:
            add(build([action], []))
        add(build([], purchase))
        out.append({})
        return out


def _plain(name: str) -> str:
    """A topic as plain words: Meta's names carry a qualifier ("Education (social concept)") that TikTok's
    keyword search does not know."""
    return re.sub(r"\s*\([^)]*\)", "", name or "").strip()


def _entries(node: Any, id_keys: Iterable[str], name_keys: Iterable[str], keep_raw: bool = False) -> list[dict]:
    """Every {"id", "name"} found anywhere in a lookup response: an object with one of the id keys and one of the name
    keys, however deeply nested, first occurrence of an id (within its action_scene) winning."""
    found: dict[str, dict] = {}

    def walk(n: Any) -> None:
        if isinstance(n, dict):
            ident = next((n[k] for k in id_keys if n.get(k) not in (None, "")), None)
            scene = n.get("action_scene") if isinstance(n.get("action_scene"), str) else ""
            name = next((n[k] for k in name_keys if isinstance(n.get(k), str) and n[k].strip()), None)
            if ident is not None and name:
                entry = {"id": str(ident), "name": name.strip()}
                if keep_raw:   # the entry's other plain fields, for telling categories apart
                    entry["raw"] = {k: v for k, v in n.items() if isinstance(v, (str, int, float, bool))}
                found.setdefault(f"{scene}|{ident}", entry)   # ids are only unique within a scene
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    walk(node)
    return list(found.values())


async def _lookup(path: str, params: dict, access_token: str) -> Optional[dict]:
    """GET a /tool/ endpoint; its "data", or None on any failure (these are optional extras, so never raise)."""
    api_base = f"{settings.TIKTOK_ADS_API_BASE}/open_api/{settings.TIKTOK_ADS_API_VERSION}"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                f"{api_base}{path}",
                headers={"Access-Token": access_token, "Content-Type": "application/json"},
                params=params,
            )
            data = resp.json()
    except Exception as e:
        print(f"[TikTokAdsAdapter] {path} lookup failed: {e}", flush=True)
        return None
    if data.get("code") not in (0, None):
        print(f"[TikTokAdsAdapter] {path} rejected: {data.get('message')}", flush=True)
        return None
    return data.get("data") or {}


def _match(names: list[str], pool: list[dict], best: Callable[[str, list[dict]], Optional[dict]], limit: int) -> list[str]:
    """Ids of the pool entries that best fit each interest name. `best` is the adapter's own interest matcher, which
    reads interest_category_id / interest_category_name, so entries are handed over in that shape."""
    shaped = [{"interest_category_id": e["id"], "interest_category_name": e["name"]} for e in pool]
    ids: list[str] = []
    for name in names:
        hit = best(name, shaped)
        if hit and hit["interest_category_id"] not in ids:
            ids.append(hit["interest_category_id"])
        if len(ids) >= limit:
            break
    return ids


async def resolve_behavior_targeting(
    interest_names: list[str],
    advertiser_id: str,
    access_token: str,
    best_match: Callable[[str, list[dict]], Optional[dict]],
    want_purchase_intent: bool = False,
) -> BehaviorTargeting:
    """Video / creator / hashtag interactions (and, when `want_purchase_intent`, purchase intention) for the same
    topics Jane chose as interests. With no topics there is nothing to look up and nothing is added."""
    result = BehaviorTargeting()
    names = [n for n in interest_names if n][:10]
    if not names:
        return result

    # Video and creator interactions both use /tool/action_category/, which lists the two kinds together; each entry's
    # action_scene says which, and each scene is matched against its own entries only.
    data = await _lookup("/tool/action_category/", {"advertiser_id": advertiser_id}, access_token)
    if data is not None:
        pool = _entries(data, ("action_category_id", "id"), ("action_category_name", "name"), keep_raw=True)
        for scene, actions, period in (("VIDEO_RELATED", VIDEO_ACTIONS, VIDEO_ACTION_DAYS),
                                       ("CREATOR_RELATED", CREATOR_ACTIONS, 0)):
            scoped = [e for e in pool if (e.get("raw") or {}).get("action_scene") == scene]
            category_ids = _match(names, scoped, best_match, MAX_ACTION_CATEGORIES)
            by_id = {e["id"]: e["name"] for e in scoped}
            print(f"[TikTokAdsAdapter] {scene} categories: {len(scoped)} of {len(pool)} read; matched "
                  f"{[(i, by_id.get(i)) for i in category_ids]} for {names}", flush=True)
            if not category_ids:
                # So a miss can be told from "TikTok has no such category": what the scene did offer.
                print(f"[TikTokAdsAdapter] {scene} had no match; its categories: {[e['name'] for e in scoped][:100]}",
                      flush=True)
            if category_ids:
                result.actions.append({
                    "action_scene": scene, "action_period": period,
                    "video_user_actions": actions, "action_category_ids": category_ids,
                })

    keywords = list(dict.fromkeys(k for k in (_plain(n) for n in names) if k))[:5]
    data = await _lookup(
        "/tool/hashtag/recommend/",
        {"advertiser_id": advertiser_id, "keywords": json.dumps(keywords), "operator": "OR"}, access_token)
    if data is not None:
        hashtag_keys = (("hashtag_id", "keyword_id", "id"), ("hashtag_name", "keyword", "name"))
        hashtags = _entries(data, *hashtag_keys)
        if not hashtags:
            # Several keywords at once can come back empty where a single one does not, and a hashtag is one token:
            # "Social media marketing" finds nothing, "marketing" and "socialmediamarketing" do. So try each topic's
            # key word on its own first, then the topic run together.
            singles = list(dict.fromkeys(k.split()[-1].lower() for k in keywords if k.split()))
            joined = [k.replace(" ", "").lower() for k in keywords if " " in k]
            for candidate in list(dict.fromkeys(singles + joined))[:7]:
                if len(hashtags) >= MAX_HASHTAGS:
                    break
                one = await _lookup("/tool/hashtag/recommend/",
                                    {"advertiser_id": advertiser_id, "keywords": json.dumps([candidate])}, access_token)
                for entry in _entries(one or {}, *hashtag_keys)[:2]:
                    if entry["id"] not in {h["id"] for h in hashtags}:
                        hashtags.append(entry)
        if not hashtags:
            print(f"[TikTokAdsAdapter] no hashtags returned for {keywords}; TikTok answered {str(data)[:300]}",
                  flush=True)
        if hashtags:
            result.actions.append({
                "action_scene": "HASHTAG_RELATED", "action_period": 0,
                "video_user_actions": HASHTAG_ACTIONS,
                "action_category_ids": [h["id"] for h in hashtags[:MAX_HASHTAGS]],
            })

    if want_purchase_intent:
        ids: list[str] = []
        # Plain words (Meta's "Parenting (children and parenting)" matches nothing), fuzzy match first, then TikTok's
        # semantic search for what the fuzzy match could not find.
        for keyword in keywords[:3]:
            for mode in ("FUZZ_MATCH", "SEMANTIC_RECOMMEND"):
                data = await _lookup(
                    "/tool/interest_keyword/recommend/",
                    {"advertiser_id": advertiser_id, "keyword": keyword, "audience_type": "PURCHASE_INTENTION",
                     "mode": mode, "limit": 3}, access_token)
                found = _entries(data or {}, ("keyword_id", "id"), ("keyword", "name"))
                if not found:
                    print(f"[TikTokAdsAdapter] no purchase intention keywords for {keyword!r} ({mode}); TikTok answered "
                          f"{str(data)[:300]}", flush=True)
                    continue
                ids.extend(e["id"] for e in found if e["id"] not in ids)
                break
        result.purchase_intention_keyword_ids = ids[:MAX_PURCHASE_INTENT]
    return result
