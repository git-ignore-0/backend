#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
test_root="$(mktemp -d)"
trap 'rm -rf -- "$test_root"' EXIT
mkdir -p "$test_root/project/scripts" "$test_root/bundle" "$test_root/bin"
cp "$project_root/scripts/deploy-test-ftps.sh" "$test_root/project/scripts/"
printf 'server\n' >"$test_root/bundle/manage.py"
printf '{}\n' >"$test_root/bundle/passenger_wsgi.py"
(
  cd "$test_root/bundle"
  zip -qr "$test_root/project/backend-release.zip" .
)
cat >"$test_root/bin/lftp" <<'MOCK'
#!/usr/bin/env bash
set -euo pipefail
test "$LFTP_PASSWORD" = 'secret with spaces $ and quotes'
printf '%s\n' "$@" >>"$TEST_OUTPUT/arguments"
commands="$(cat)"
printf '%s\n' "$commands" >>"$TEST_OUTPUT/commands"
if [[ "$commands" == *'get worker.json'* ]]; then
  python3 -c 'import json,time;open("worker.json","w").write(json.dumps({"protocol":1,"app":"backend-test","time":time.time()}))'
elif [[ "$commands" == *'.result.json -o result.json'* ]]; then
  if [[ ! -f "$TEST_OUTPUT/interrupted" ]]; then
    printf '{"release":' >result.json
    touch "$TEST_OUTPUT/interrupted"
    exit 1
  fi
  test ! -f result.json
  python3 -c 'import json;data=json.load(open("request.json"));data["status"]="deployed";open("result.json","w").write(json.dumps(data))'
fi
MOCK
chmod +x "$test_root/bin/lftp"
printf '#!/usr/bin/env bash\nexit 0\n' >"$test_root/bin/sleep"
chmod +x "$test_root/bin/sleep"
export PATH="$test_root/bin:$PATH"
export TEST_OUTPUT="$test_root"
export TEST_FTP_SERVER=epsilon.kc.epik.com
export TEST_FTP_USERNAME=deploy_be_test@forvietnam.org
export TEST_FTP_PASSWORD='secret with spaces $ and quotes'
export GITHUB_SHA=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
export GITHUB_RUN_ID=123
export GITHUB_RUN_ATTEMPT=1
bash "$test_root/project/scripts/deploy-test-ftps.sh"
grep -q 'set ftp:ssl-force yes' "$test_root/commands"
grep -q 'set ftp:ssl-protect-data yes' "$test_root/commands"
grep -q 'set ssl:verify-certificate yes' "$test_root/commands"
grep -q 'set ssl:check-hostname yes' "$test_root/commands"
grep -qF 'ftp://epsilon.kc.epik.com' "$test_root/arguments"
! grep -qE 'mirror|put .*restart.txt|--delete' "$test_root/commands"
test "$(grep -c '^put bundle.zip' "$test_root/commands")" = 1
grep -q '\.ready.part .*\.ready.json$' "$test_root/commands"
! grep -qF "$TEST_FTP_PASSWORD" "$test_root/arguments"
rm "$test_root/commands"
if TEST_FTP_USERNAME=production bash "$test_root/project/scripts/deploy-test-ftps.sh" 2>/dev/null; then
  echo 'Expected non-test credentials to be rejected.' >&2
  exit 1
fi
test ! -f "$test_root/commands"
echo 'Backend ZIP FTPS deployment checks passed.'
