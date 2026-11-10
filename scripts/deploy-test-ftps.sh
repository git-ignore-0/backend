#!/usr/bin/env bash
set -euo pipefail

: "${TEST_FTP_SERVER:?Missing TEST_FTP_SERVER}"
: "${TEST_FTP_USERNAME:?Missing TEST_FTP_USERNAME}"
: "${TEST_FTP_PASSWORD:?Missing TEST_FTP_PASSWORD}"
if [[ "$TEST_FTP_SERVER" != "epsilon.kc.epik.com" ||
  "$TEST_FTP_USERNAME" != "deploy_be_test@forvietnam.org" ]]; then
  echo 'Expected FTPS host epsilon.kc.epik.com and the dedicated Backend test account.' >&2
  exit 1
fi

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
release="${GITHUB_SHA:-$(git -C "$project_root" rev-parse HEAD)}-${GITHUB_RUN_ID:-$(date -u +%s)}-${GITHUB_RUN_ATTEMPT:-0}"
[[ "$release" =~ ^[a-f0-9]{40}-[0-9]+-[0-9]+$ ]]
deploy_work_root="$(mktemp -d)"
trap 'rm -rf -- "$deploy_work_root"' EXIT
cp "$project_root/backend-release.zip" "$deploy_work_root/bundle.zip"
cd "$deploy_work_root"
python3 - "$release" <<'PY'
import hashlib, json, sys, zipfile
from pathlib import Path
with zipfile.ZipFile('bundle.zip') as archive:
    names = archive.namelist()
    assert 'manage.py' in names and 'passenger_wsgi.py' in names
    assert not any(any(p.startswith('.env') or p == '.htaccess' for p in n.split('/')) for n in names if n != '.env.example')
    assert archive.testzip() is None
with open('bundle.zip', 'rb') as stream:
    hasher = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
        hasher.update(chunk)
    digest = hasher.hexdigest()
Path('request.json').write_text(json.dumps({'release': sys.argv[1], 'sha256': digest}))
print(f'ZIP: {Path("bundle.zip").stat().st_size / 1024**2:.1f} MiB; release: {sys.argv[1]}', flush=True)
PY

ftps() {
  {
    cat <<'CONFIG'
set cmd:fail-exit yes
set ftp:ssl-force yes
set ftp:ssl-protect-data yes
set ssl:verify-certificate yes
set ssl:check-hostname yes
set net:timeout 30
set net:max-retries 2
set net:reconnect-interval-base 5
cd /tmp/nfv-deploy
CONFIG
    cat
  } | LFTP_PASSWORD="$TEST_FTP_PASSWORD" timeout 600 lftp --norc \
    --env-password --user "$TEST_FTP_USERNAME" -p 21 "ftp://$TEST_FTP_SERVER"
}

echo 'Checking hosting deploy worker...'
if ! ftps <<'LFTP'
get worker.json -o worker.json
bye
LFTP
then
  echo 'Install and run be-test-deploy-worker.py on hosting before deploying.' >&2
  exit 1
fi
python3 - <<'PY'
import json, time
from pathlib import Path
worker = json.loads(Path('worker.json').read_text())
assert worker['protocol'] == 1 and worker.get('app') == 'backend-test', 'Wrong hosting worker'
assert -60 < time.time() - worker['time'] < 180, 'Hosting worker heartbeat expired; check cron'
PY

echo 'Uploading one ZIP over FTPS (10-minute upload limit)...'
ftps <<LFTP
put bundle.zip -o $release.zip.part
mv $release.zip.part $release.zip
put request.json -o $release.ready.part
mv $release.ready.part $release.ready.json
bye
LFTP
echo 'ZIP uploaded. Waiting for hosting cron (normally within one minute)...'

for attempt in $(seq 1 120); do
  # A failed RETR can leave partial bytes; lftp does not clobber local files by default.
  rm -f result.json
  if ftps <<LFTP
get $release.result.json -o result.json
bye
LFTP
  then
    python3 - <<'PY'
import json
from pathlib import Path
request = json.loads(Path('request.json').read_text())
result = json.loads(Path('result.json').read_text())
assert result['release'] == request['release'], 'Wrong release receipt'
if result['status'] != 'deployed':
    raise SystemExit('Hosting deploy failed: ' + result.get('error', 'unknown error'))
assert result['sha256'] == request['sha256'], 'Wrong deployed ZIP checksum'
print('Hosting installed the verified ZIP and requested Passenger restart.', flush=True)
PY
    exit 0
  fi
  rm -f result.json
  echo "Hosting receipt pending ($attempt/120)."
  sleep 10
done
echo 'No hosting receipt after polling. Check cron and worker log; do not assume deployment succeeded.' >&2
exit 1
