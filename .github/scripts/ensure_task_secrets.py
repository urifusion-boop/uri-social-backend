"""Add required secrets to the task definition the deploy is about to register.

Secrets live on the registered task definition, not in this repo, and the deploy
uses the CURRENT one as its base. So a newly added SSM parameter never reaches the
container until something puts it on that base — this does. Idempotent.
"""
from __future__ import annotations

import json
import sys

CONTAINER = "social-backend-service-prod-v1"
SSM_PREFIX = "arn:aws:ssm:eu-west-1:062677866324:parameter/uri/social-backend/prod"

REQUIRED = {
    "META_WEBHOOK_VERIFY_TOKEN": f"{SSM_PREFIX}/META_WEBHOOK_VERIFY_TOKEN",
    "TIKTOK_ADS_ADVERTISER_ID": f"{SSM_PREFIX}/TIKTOK_ADS_ADVERTISER_ID",
    "TIKTOK_ADS_ACCESS_TOKEN": f"{SSM_PREFIX}/TIKTOK_ADS_ACCESS_TOKEN",
    "TIKTOK_ADS_APP_ID": f"{SSM_PREFIX}/TIKTOK_ADS_APP_ID",
    "TIKTOK_ADS_APP_SECRET": f"{SSM_PREFIX}/TIKTOK_ADS_APP_SECRET",
}


def ensure(task_definition: dict, container_name: str, required: dict) -> list[str]:
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
        environment[:] = [e for e in environment if e.get("name") != name]
        if name not in present:
            secrets.append({"name": name, "valueFrom": arn})
            added.append(name)
    return added


def main(path: str) -> None:
    with open(path) as f:
        task_definition = json.load(f)

    added = ensure(task_definition, CONTAINER, REQUIRED)

    with open(path, "w") as f:
        json.dump(task_definition, f)

    print(f"added: {', '.join(added)}" if added else "added: nothing (already present)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "task-definition.json")
