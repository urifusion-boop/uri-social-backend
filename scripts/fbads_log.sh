#!/usr/bin/env bash
# Recent Facebook-Ads OAuth outcomes in prod, newest last.
#   bash scripts/fbads_log.sh            # last 3 days
#   bash scripts/fbads_log.sh 7          # last 7 days
set -o pipefail

DAYS="${1:-3}"
SINCE=$(python3 -c "import time,sys;print(int((time.time()-86400*int(sys.argv[1]))*1000))" "$DAYS")

AWS_PROFILE=uri-prod aws logs filter-log-events \
  --log-group-name "ecs/fargate/service/prod/social-backend-service-service" \
  --start-time "$SINCE" \
  --filter-pattern "FBAdsOAuth" \
  --max-items 60 \
  --query "events[].[timestamp,message]" \
  --output text |
python3 -c '
import sys, datetime
for line in sys.stdin:
    parts = line.split("\t", 1)
    if len(parts) > 1 and parts[0].strip().isdigit():
        when = datetime.datetime.fromtimestamp(int(parts[0]) / 1000)
        print(when.strftime("%m-%d %H:%M"), parts[1].strip()[:200])
'
