#!/usr/bin/env bash
# Runs the gowui image the way a deployment does and checks it (SPEC §10.1, issue #10).
#
#   tools/container_smoke.sh IMAGE
#
# One container on a named volume, with a read-only root filesystem, a tmpfs at /tmp, every
# capability dropped and no-new-privileges. Checks: /healthz answers 200 {"ok": true}; Docker's
# health check reaches healthy; the process is not root and the database is under /data;
# `gowui user add` works through `docker exec`; `docker stop` ends it with status 0; the account
# survives a restart on the same volume; the image declares VOLUME /data; and a bad configuration
# refuses to start. Needs docker and curl.
set -euo pipefail

image=${1:?usage: tools/container_smoke.sh IMAGE}
name="gowui-smoke-$$"
volume="gowui-smoke-data-$$"
body=$(mktemp)
hardening=(--read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges)

cleanup() {
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker volume rm -f "$volume" >/dev/null 2>&1 || true
  rm -f "$body"
}
trap cleanup EXIT

ok() { echo "container_smoke: ok: $*"; }
fail() {
  echo "container_smoke: FAIL: $*" >&2
  docker logs "$name" >&2 2>&1 || true
  exit 1
}

volumes=$(docker image inspect --format '{{json .Config.Volumes}}' "$image")
[[ $volumes == *'"/data"'* ]] || fail "the image does not declare VOLUME /data ($volumes)"
ok "the image declares VOLUME /data"

docker volume create "$volume" >/dev/null
docker run -d --name "$name" -v "$volume:/data" -p 127.0.0.1::8080 "${hardening[@]}" \
  --health-interval 1s --health-timeout 3s --health-retries 3 "$image" >/dev/null

port=$(docker port "$name" 8080/tcp | head -n 1)
port=${port##*:}
[[ $port =~ ^[0-9]+$ ]] || fail "no published port"

status=""
for _ in $(seq 60); do
  status=$(curl -sS -o "$body" -w '%{http_code}' "http://127.0.0.1:$port/healthz" 2>/dev/null) \
    && [[ $status == 200 ]] && break
  sleep 1
done
[[ $status == 200 ]] || fail "/healthz answered '$status'"
[[ $(tr -d ' \n' <"$body") == '{"ok":true}' ]] || fail "/healthz body: $(cat "$body")"
ok '/healthz answers 200 {"ok": true}'

health=""
for _ in $(seq 60); do
  health=$(docker inspect --format '{{.State.Health.Status}}' "$name")
  [[ $health == healthy ]] && break
  sleep 1
done
[[ $health == healthy ]] || fail "health is '$health'"
ok "Docker health is healthy"

uid=$(docker exec "$name" id -u)
[[ $uid =~ ^[0-9]+$ && $uid != 0 ]] || fail "runs as uid '$uid'"
docker exec "$name" test -f /data/gowui.db || fail "/data/gowui.db is missing"
ok "runs as uid $uid; the database is /data/gowui.db"

added=$(printf 'correct horse battery\n' \
  | docker exec -i "$name" gowui user add alice --password-stdin)
[[ $added == ok ]] || fail "user add printed '$added'"
ok "gowui user add alice via docker exec -i"

docker stop "$name" >/dev/null
code=$(docker inspect --format '{{.State.ExitCode}}' "$name")
[[ $code == 0 ]] || fail "docker stop ended the container with status $code"
ok "docker stop exits 0"

docker start "$name" >/dev/null
users=$(docker exec "$name" gowui user list)
grep -qx alice <<<"$users" || fail "alice is gone after a restart (users: $users)"
ok "alice survives a restart on the same volume"

if docker run --rm "${hardening[@]}" -e GOWUI_AUTH=header "$image" >/dev/null 2>&1; then
  fail "GOWUI_AUTH=header without GOWUI_TRUSTED_PROXIES started"
fi
ok "a malformed configuration refuses to start"

echo "container_smoke: all checks passed for $image"
