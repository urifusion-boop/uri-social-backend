"""
TikTok audience targeting by what people DO, not only what they are into.

Ads Manager's Audience > Interests & behaviors box has more than "Interests": Purchase intention, Video
interactions, Creator interactions and Hashtag interactions (creator interactions are not sent, see below). The ad group only ever set interest_category_ids, so a
Jane ad reached everyone interested in the topic and ignored the people who had just watched, followed or searched it.

Field names and limits below are from TikTok's own Marketing API SDK (adgroup/create):
  * actions[]  {action_scene, action_period, video_user_actions, action_category_ids}. action_scene is VIDEO_RELATED
    (watched / liked / commented / shared, over 7 or 15 days), CREATOR_RELATED (follows / visits a creator's page) or
    HASHTAG_RELATED (viewed a hashtag). Valid only when TikTok is the only placement, which is how Jane launches.
    Video ids come from GET /tool/action_category/, hashtag ids from GET /tool/hashtag/recommend/. Live on staging
    (2026-11-11): VIDEO_RELATED accepted; CREATOR_RELATED rejected with those same ids, so it is left out.
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
        """The field sets to try for the ad group, widest first, ending with none: everything, then the interactions
        without purchase intention, then one interaction at a time, then purchase intention alone. A rejected create
        leaves nothing behind, so one audience TikTok does not accept costs only the others' retries, not the launch."""
        out: list = []

        def add(d: dict) -> None:
            if d and d not in out:
                out.append(d)

        add(self.fields())
        add({"actions": self.actions} if self.actions else {})
        for action in self.actions:
            add({"actions": [action]})
        add({"purchase_intention_keyword_ids": self.purchase_intention_keyword_ids}
            if self.purchase_intention_keyword_ids else {})
        out.append({})
        return out


def _plain(name: str) -> str:
    """A topic as plain words: Meta's names carry a qualifier ("Education (social concept)") that TikTok's
    keyword search does not know."""
    return re.sub(r"\s*\([^)]*\)", "", name or "").strip()


def _entries(node: Any, id_keys: Iterable[str], name_keys: Iterable[str], keep_raw: bool = False) -> list[dict]:
    """Every {"id", "name"} found anywhere in a lookup response: an object with one of the id keys and one of the name
    keys, however deeply nested, first occurrence of an id winning."""
    found: dict[str, dict] = {}

    def walk(n: Any) -> None:
        if isinstance(n, dict):
            ident = next((n[k] for k in id_keys if n.get(k) not in (None, "")), None)
            name = next((n[k] for k in name_keys if isinstance(n.get(k), str) and n[k].strip()), None)
            if ident is not None and name:
                entry = {"id": str(ident), "name": name.strip()}
                if keep_raw:   # the entry's other plain fields, for telling categories apart
                    entry["raw"] = {k: v for k, v in n.items() if isinstance(v, (str, int, float, bool))}
                found.setdefault(str(ident), entry)
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


def _describe_pool(pool: list[dict]) -> None:
    """Log which plain fields the category entries carry and the values they take, so video categories can be told
    from creator categories (TikTok lists both in one response but the API has no scene filter)."""
    fields: dict[str, set] = {}
    for e in pool:
        for k, v in (e.get("raw") or {}).items():
            if k not in ("action_category_id", "action_category_name", "id", "name"):
                fields.setdefault(k, set()).add(v)
    summary = {k: sorted(map(str, vals))[:8] for k, vals in fields.items()}
    print(f"[TikTokAdsAdapter] action category fields (value samples): {summary}; "
          f"first entries {[e.get('raw') for e in pool[:3]]}", flush=True)


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

    # Video interactions use /tool/action_category/ (confirmed live: TikTok accepts them). CREATOR_RELATED is NOT sent:
    # TikTok rejected it ("Invalid behavior targeting category") with the very same ids the video scene accepted, and
    # its API has no separate creator-category list to take ids from.
    data = await _lookup("/tool/action_category/", {"advertiser_id": advertiser_id}, access_token)
    if data is not None:
        pool = _entries(data, ("action_category_id", "id"), ("action_category_name", "name"), keep_raw=True)
        _describe_pool(pool)
        category_ids = _match(names, pool, best_match, MAX_ACTION_CATEGORIES)
        by_id = {e["id"]: e["name"] for e in pool}
        print(f"[TikTokAdsAdapter] action categories: {len(pool)} read; matched "
              f"{[(i, by_id.get(i)) for i in category_ids]} for {names}", flush=True)
        if not category_ids:
            print(f"[TikTokAdsAdapter] no video/creator category matched {names} "
                  f"({len(pool)} categories read from TikTok)", flush=True)
        if category_ids:
            result.actions.append({
                "action_scene": "VIDEO_RELATED", "action_period": VIDEO_ACTION_DAYS,
                "video_user_actions": VIDEO_ACTIONS, "action_category_ids": category_ids,
            })

    keywords = list(dict.fromkeys(k for k in (_plain(n) for n in names) if k))[:5]
    data = await _lookup(
        "/tool/hashtag/recommend/",
        {"advertiser_id": advertiser_id, "keywords": json.dumps(keywords), "operator": "OR"}, access_token)
    if data is not None:
        hashtag_keys = (("hashtag_id", "keyword_id", "id"), ("hashtag_name", "keyword", "name"))
        hashtags = _entries(data, *hashtag_keys)
        if not hashtags:
            # Several keywords at once can come back empty where a single one does not.
            for keyword in keywords[:3]:
                one = await _lookup("/tool/hashtag/recommend/",
                                    {"advertiser_id": advertiser_id, "keywords": json.dumps([keyword])}, access_token)
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
        for name in names[:3]:
            data = await _lookup(
                "/tool/interest_keyword/recommend/",
                {"advertiser_id": advertiser_id, "keyword": name, "audience_type": "PURCHASE_INTENTION", "limit": 3},
                access_token)
            for entry in _entries(data or {}, ("keyword_id", "id"), ("keyword", "name")):
                if entry["id"] not in ids:
                    ids.append(entry["id"])
        result.purchase_intention_keyword_ids = ids[:MAX_PURCHASE_INTENT]
    return result
