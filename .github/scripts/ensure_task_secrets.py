"""Add required secrets to the task definition the deploy is about to register.

Secrets live on the registered task definition, not in this repo, and the deploy
uses the CURRENT one as its base. So a newly added SSM parameter never reaches the
container until something puts it on that base — this does.

Idempotent by design: an entry that is already there is left alone, so this stays
in the pipeline permanently instead of being a one-off manual step somebody has to
remember after the next task definition is rebuilt.
"""
from __future__ import annotations

import json
import sys

CONTAINER = "social-backend-service-prod-v1"
SSM_PREFIX = "arn:aws:ssm:eu-west-1:062677866324:parameter/uri/social-backend/prod"

REQUIRED = {
    # The Unified Inbox webhook handshake. Without it Meta cannot subscribe, so
    # no message or comment ever arrives.
    "META_WEBHOOK_VERIFY_TOKEN": f"{SSM_PREFIX}/META_WEBHOOK_VERIFY_TOKEN",
    # AI video creation (storyboard writing on Gemini 3.5 Flash-Lite, scene
    # animation on Veo 3.1) — confirmed missing/invalid live 2026-09-30:
    # generate-storyboard returned Google's own "API key not valid" error the
    # first time this path was ever exercised against the deployed container
    # (the feature's UI tab has been hidden since Jul 31, so nothing had
    # called it live before). The SSM parameter itself still needs a real key
    # value put there by whoever manages this project's secrets — this entry
    # only makes sure that value reaches the container once it exists.
    "GOOGLE_GEMINI_API_KEY": f"{SSM_PREFIX}/GOOGLE_GEMINI_API_KEY",
    # Market Intelligence (social listening) collection through Apify actors —
    # the real source adapters (X first) read it; the mock adapter needs none.
    "APIFY_API_TOKEN": f"{SSM_PREFIX}/APIFY_API_TOKEN",
    # Nigerian voices in talking videos (YarnGPT hosted text-to-speech). Without it those voices
    # use the older voice-cloning path, exactly as before — so a missing value is safe, but the
    # SSM parameter must EXIST before this line ships or the task cannot start.
    "YARNGPT_API_KEY": f"{SSM_PREFIX}/YARNGPT_API_KEY",
    # Credits for AI video: off | shadow | on (see video_generation_billing.py). The backend treats a missing
    # or unrecognised value as "off", but the SSM parameter must EXIST before this line ships or the task
    # cannot start.
    "VIDEO_GENERATION_BILLING_MODE": f"{SSM_PREFIX}/VIDEO_GENERATION_BILLING_MODE",
    # Minimum seconds between two Instagram/Facebook posts to the same account (rate_limit.py). The code
    # defaults to 5 when it is absent, but the SSM parameter must EXIST before this line ships or the task
    # cannot start. Raise it on dev to see Approve answer "queued" instead of waiting.
    "META_MIN_GAP_S": f"{SSM_PREFIX}/META_MIN_GAP_S",
    # Minimum seconds between two TikTok posts to the same account (rate_limit.py). The code defaults to 12
    # when it is absent, but the SSM parameter must EXIST before this line ships or the task cannot start.
    "TIKTOK_MIN_GAP_S": f"{SSM_PREFIX}/TIKTOK_MIN_GAP_S",
    "TIKTOK_ADS_ADVERTISER_ID": f"{SSM_PREFIX}/TIKTOK_ADS_ADVERTISER_ID",
    "TIKTOK_ADS_ACCESS_TOKEN": f"{SSM_PREFIX}/TIKTOK_ADS_ACCESS_TOKEN",
    "TIKTOK_ADS_APP_ID": f"{SSM_PREFIX}/TIKTOK_ADS_APP_ID",
    "TIKTOK_ADS_APP_SECRET": f"{SSM_PREFIX}/TIKTOK_ADS_APP_SECRET",
}

# Plain, non-secret config — a literal value on the task definition's
# `environment` list, not an SSM-backed `secrets` entry (nothing here is
# sensitive: a bucket name and a region string). Added for the Cloudinary->S3
# media migration — app/utils/s3_upload.py reads these via os.environ.
REQUIRED_ENV = {
    "S3_MEDIA_BUCKET": "uri-social-media-prod",
    "AWS_REGION": "eu-west-1",
    # Google verified URI's OAuth app for the Google Ads scope, so "Continue with Google"
    # is offered instead of leading with the Customer ID route (see config.py).
    "GOOGLE_ADS_OAUTH_VERIFIED": "true",
}


def ensure(task_definition: dict, container_name: str, required: dict, required_env: dict) -> list[str]:
    container = next(
        (c for c in task_definition.get("containerDefinitions", [])
         if c.get("name") == container_name),
        None,
    )
    if container is None:
        raise SystemExit(f"container {container_name!r} not found in the task definition")

    secrets = container.setdefault("secrets", [])
    environment = container.setdefault("environment", [])
    present = {s.get("name") for s in secrets}
    added = []
    for name, arn in required.items():
        # A name can't live in both `environment` and `secrets` on the same
        # container — ECS's RegisterTaskDefinition rejects that outright. If an
        # earlier manual step left this as a plaintext env var, drop it here so
        # the SSM-backed secret is the only source of truth going forward.
        environment[:] = [e for e in environment if e.get("name") != name]
        if name not in present:
            secrets.append({"name": name, "valueFrom": arn})
            added.append(name)

    env_present = {e.get("name") for e in environment}
    for name, value in required_env.items():
        if name not in env_present:
            environment.append({"name": name, "value": value})
            added.append(name)
    return added


def main(path: str) -> None:
    with open(path) as f:
        task_definition = json.load(f)

    added = ensure(task_definition, CONTAINER, REQUIRED, REQUIRED_ENV)

    with open(path, "w") as f:
        json.dump(task_definition, f)

    print(f"added: {', '.join(added)}" if added else "added: nothing (already present)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "task-definition.json")
