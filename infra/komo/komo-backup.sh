#!/bin/bash
# Komo D1 volume backup: consistent SQLite snapshot -> tar.gz -> gpg symmetric -> off-box rsync (Hetzner Ashburn).
# Retention: 30 days remote, 7 days local staging. Secrets only via files, never printed.
set -euo pipefail
umask 077
VOL=/var/lib/docker/volumes/komo-design-feedback-udvxd1_komo-data/_data
PASS=/root/.komo-backup.pass
KEY=/root/.ssh/komo_backup_ed25519
REMOTE=root@5.161.224.50
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
WORK=$(mktemp -d /var/tmp/komo-backup.XXXXXX)
trap 'rm -rf "$WORK"' EXIT
OUT=/var/backups/komo
mkdir -p "$OUT"

python3 - "$VOL" "$WORK" <<'PY'
import sqlite3, sys, glob, os
vol, work = sys.argv[1:3]
os.makedirs(work + '/runtime/v3/d1/miniflare-D1DatabaseObject')
for src in glob.glob(vol + '/runtime/v3/d1/miniflare-D1DatabaseObject/*.sqlite'):
    dst = work + '/runtime/v3/d1/miniflare-D1DatabaseObject/' + os.path.basename(src)
    s = sqlite3.connect('file:' + src + '?mode=ro', uri=True)
    d = sqlite3.connect(dst)
    s.backup(d)
    ok = d.execute('pragma integrity_check').fetchone()[0]
    d.close(); s.close()
    if ok != 'ok':
        raise SystemExit('integrity_check failed for ' + src)
PY

tar -C "$WORK" -czf "$WORK/komo.tar.gz" runtime
FILE="komo-$STAMP.tar.gz.gpg"
gpg --batch --yes --pinentry-mode loopback --passphrase-file "$PASS" \
    --symmetric --cipher-algo AES256 -o "$OUT/$FILE" "$WORK/komo.tar.gz"
sha256sum "$OUT/$FILE" | cut -d' ' -f1 > "$OUT/$FILE.sha256"

rsync -e "ssh -i $KEY -o BatchMode=yes -o IdentitiesOnly=yes -o ConnectTimeout=20" \
    -t "$OUT/$FILE" "$OUT/$FILE.sha256" "$REMOTE:/"
# verify remote copy size matches
LOCAL_SZ=$(stat -c %s "$OUT/$FILE")
REMOTE_LS=$(rsync -e "ssh -i $KEY -o BatchMode=yes -o IdentitiesOnly=yes" --list-only "$REMOTE:/$FILE" | awk '{gsub(",","",$2);print $2}')
[ "$LOCAL_SZ" = "$REMOTE_LS" ] || { echo "remote size mismatch" >&2; exit 1; }

# retention: remote 30 days (delete by name via rrsync is not allowed; pruned by remote cron). Local 7 days.
find "$OUT" -name 'komo-*.gpg*' -mtime +7 -delete
echo "komo-backup ok $FILE bytes=$LOCAL_SZ"
