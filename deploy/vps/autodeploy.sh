#!/usr/bin/env bash
# Deploy the checkout's commit to the VPS. Komodo's take2-riley stack runs this as its pre-deploy
# (pukujan-lab-2/server-management komodo/resources/take2-riley.toml), after its [[repo]] has pulled main.
#
# Builds the commit as take2-riley:<12-char sha>, swaps the worker and the intake page to it, waits for it to register with LiveKit and
# puts the previous image back if it doesn't. Either way it writes deploy/.env, which compose reads by itself, so
# Komodo's `compose up` afterwards resolves the same image and changes nothing.
# Once it is registered, the nurse-screen repo's old worker (same agent name) is stopped, so calls come only here.
# A commit that failed is remembered in .deploy-failed and not retried every 3 minutes; delete that file to retry it.
set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_DIR="$(cd "$DEPLOY_DIR/.." && pwd)"
COMPOSE=(docker compose -f "$DEPLOY_DIR/docker-compose.vps.yml" -p take2-riley)

# The clone is root's (Komodo's agent runs git as root), so safe.directory lets any user read it
SHA="$(git -c safe.directory="$REPO_DIR" -C "$REPO_DIR" rev-parse HEAD)"
TAG="${SHA:0:12}"
RUNNING="$(docker inspect take2-riley --format '{{.Config.Image}}' 2>/dev/null || true)"
write_tag() { printf 'TAG=%s\n' "$1" > "$DEPLOY_DIR/.env"; }
retire_old() {
  if [ "$(docker inspect nurse-screen --format '{{.State.Running}}' 2>/dev/null)" = "true" ]; then
    docker stop nurse-screen >/dev/null && echo "stopped the old nurse-screen worker"
  fi
}

WEB="$(docker inspect take2-riley-web --format '{{.Config.Image}}' 2>/dev/null || true)"
if [ "$RUNNING" = "take2-riley:$TAG" ] && [ "$WEB" = "take2-riley:$TAG" ]; then
  echo "take2-riley already runs $TAG"
  write_tag "$TAG"
  retire_old
  exit 0
fi

if [ "$(cat "$DEPLOY_DIR/.deploy-failed" 2>/dev/null)" = "$SHA" ]; then
  echo "$TAG failed to deploy before; keeping ${RUNNING:-nothing} (delete deploy/.deploy-failed to retry)"
  write_tag "${RUNNING#take2-riley:}"
  exit 0
fi

mkdir -p /home/ubuntu/apps/nurse-screen-calls /home/ubuntu/apps/nurse-screen-voice
echo "main is $TAG, running ${RUNNING:-nothing}: deploying"
TAG="$TAG" "${COMPOSE[@]}" build riley
TAG="$TAG" "${COMPOSE[@]}" up -d riley web

for _ in $(seq 1 24); do
  if docker logs take2-riley 2>&1 | grep -q 'registered worker' &&
     [ "$(docker inspect take2-riley-web --format '{{.State.Health.Status}}' 2>/dev/null)" = "healthy" ]; then
    write_tag "$TAG"
    rm -f "$DEPLOY_DIR/.deploy-failed"
    echo "deployed $TAG"
    retire_old
    exit 0
  fi
  sleep 5
done

echo "$TAG did not register with LiveKit, or its intake page was not healthy, in 2 minutes; last log lines:"
docker logs --tail 30 take2-riley 2>&1 || true
docker logs --tail 15 take2-riley-web 2>&1 || true
echo "$SHA" > "$DEPLOY_DIR/.deploy-failed"
if [ -n "$RUNNING" ]; then
  TAG="${RUNNING#take2-riley:}" "${COMPOSE[@]}" up -d riley web
  write_tag "${RUNNING#take2-riley:}"
  echo "rolled back to $RUNNING"
else
  # Nothing of ours ran before: stop the failed one so the old worker keeps every call
  "${COMPOSE[@]}" stop riley web || true
fi
exit 1
