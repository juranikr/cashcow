#!/usr/bin/env bash
set -euo pipefail

image_tag="${1:?usage: container-smoke.sh <image-tag>}"
python_bin="${PYTHON_BIN:-python}"
seccomp_url="${PLAYWRIGHT_SECCOMP_URL:-https://raw.githubusercontent.com/microsoft/playwright/v1.62.0/utils/docker/seccomp_profile.json}"
seccomp_upstream_sha256="${PLAYWRIGHT_SECCOMP_UPSTREAM_SHA256:-cc3e61cabda6bbc1e53e54d27ba4d55a9d3be829b6dd1a596f4a7b31b1cc7849}"
seccomp_derived_sha256="${PLAYWRIGHT_SECCOMP_DERIVED_SHA256:-0c1bd13c078cd9402f43f6c471e5a52f9b49fe89505ecabf1daac01ff1124283}"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
run_suffix="${GITHUB_RUN_ID:-$$}-${RANDOM}"
runtime_name="cashcow-runtime-smoke-$run_suffix"
browser_name="cashcow-browser-smoke-$run_suffix"
profile_upstream="$(mktemp "${TMPDIR:-/tmp}/cashcow-playwright-seccomp-upstream.XXXXXX")"
profile="$(mktemp "${TMPDIR:-/tmp}/cashcow-playwright-seccomp.XXXXXX")"

cleanup() {
  docker rm --force "$runtime_name" "$browser_name" >/dev/null 2>&1 || true
  rm -f -- "$profile_upstream" "$profile"
}
trap cleanup EXIT

curl --fail --silent --show-error --location "$seccomp_url" --output "$profile_upstream"
"$python_bin" "$script_dir/derive-seccomp.py" \
  "$profile_upstream" "$profile" "$seccomp_upstream_sha256" "$seccomp_derived_sha256"

MSYS_NO_PATHCONV=1 LOCAL_STATE_PATH=/state/world.json RUNTIME_SERVICE_TOKEN=container-smoke-token \
docker run --detach --name "$runtime_name" --init --cap-drop ALL --read-only \
  --tmpfs /state:rw,noexec,nosuid,nodev,size=67108864 \
  --env LOCAL_STATE_PATH \
  --env RUNTIME_SERVICE_TOKEN \
  --publish 127.0.0.1::8000 \
  "cashcow-runtime:$image_tag" >/dev/null
runtime_port="$(docker port "$runtime_name" 8000/tcp | sed -E 's/^.*:([0-9]+)$/\1/' | head -1)"
runtime_health=""
for attempt in {1..45}; do
  runtime_health="$(curl --fail --silent "http://127.0.0.1:$runtime_port/cashcow/health" 2>/dev/null || true)"
  if [ -n "$runtime_health" ]; then break; fi
  sleep 2
done
if [ -z "$runtime_health" ]; then
  docker logs "$runtime_name" || true
  exit 1
fi
RUNTIME_HEALTH="$runtime_health" IMAGE_TAG="$image_tag" "$python_bin" -c \
  'import json,os; d=json.loads(os.environ["RUNTIME_HEALTH"]); assert d["status"] == "ok" and d["buildSha"] == os.environ["IMAGE_TAG"]'

docker run --detach --name "$browser_name" --init --cap-drop ALL --read-only \
  --security-opt no-new-privileges \
  --security-opt "seccomp=$profile" \
  --tmpfs /tmp:rw,nosuid,nodev,size=536870912 \
  --tmpfs /home/pwuser:rw,noexec,nosuid,nodev,size=134217728,uid=1001,gid=1001,mode=0700 \
  --shm-size 512m \
  --publish 127.0.0.1::8001 \
  "cashcow-browser-worker:$image_tag" >/dev/null
browser_port="$(docker port "$browser_name" 8001/tcp | sed -E 's/^.*:([0-9]+)$/\1/' | head -1)"
browser_health=""
for attempt in {1..45}; do
  browser_health="$(curl --fail --silent "http://127.0.0.1:$browser_port/health" 2>/dev/null || true)"
  if [ -n "$browser_health" ]; then break; fi
  sleep 2
done
if [ -z "$browser_health" ]; then
  docker logs "$browser_name" || true
  exit 1
fi
BROWSER_HEALTH="$browser_health" IMAGE_TAG="$image_tag" "$python_bin" -c \
  'import json,os; d=json.loads(os.environ["BROWSER_HEALTH"]); assert d["status"] == "ok" and d["buildSha"] == os.environ["IMAGE_TAG"] and d["credentialIsolation"] and not d["forbiddenCredentialEnvironmentPresent"] and not d["awsCredentialsPresent"] and not d["groqKeyPresent"] and d["networkProxy"] and d["pinnedHttpsProxy"] and d["safeMethodsOnly"] and d["chromiumSandboxRequired"] and d["publicHttpsOnly"]'
test "$(docker inspect "$browser_name" --format '{{.Config.User}}')" = "pwuser"

session="$(curl --fail --silent --show-error --request POST "http://127.0.0.1:$browser_port/sessions" --header 'content-type: application/json' --data '{"url":"https://example.com/","objective":"public read-only deployment smoke"}')"
SESSION_JSON="$session" "$python_bin" -c \
  'import json,os; d=json.loads(os.environ["SESSION_JSON"]); o=d["observation"]; assert d["securityBoundary"] == "isolated-public-https-read-only-worker" and o["url"].startswith("https://example.com/") and len(o["domHash"]) == 64 and len(o["screenshotSha256"]) == 64'
session_id="$(SESSION_JSON="$session" "$python_bin" -c 'import json,os; print(json.loads(os.environ["SESSION_JSON"])["sessionId"])')"
curl --fail --silent --request DELETE "http://127.0.0.1:$browser_port/sessions/$session_id" >/dev/null

echo "container smoke: PASS"
