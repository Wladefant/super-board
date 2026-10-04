#!/bin/bash
# Restore drill: pull newest backup from off-box, decrypt + restore inside a throwaway container, compare with live.
set -euo pipefail
umask 077
KEY=/root/.ssh/komo_backup_ed25519
REMOTE=root@5.161.224.50
VOL=/var/lib/docker/volumes/komo-design-feedback-udvxd1_komo-data/_data
D=$(mktemp -d /var/tmp/komo-drill.XXXXXX)
trap 'rm -rf "$D"' EXIT
SSHO="ssh -i $KEY -o BatchMode=yes -o IdentitiesOnly=yes"
F=$(rsync -e "$SSHO" --list-only "$REMOTE:/" | awk '{print $5}' | grep '\.tar\.gz\.gpg$' | sort | tail -1)
echo "restoring $F"
rsync -e "$SSHO" "$REMOTE:/$F" "$REMOTE:/$F.sha256" "$D/"
[ "$(sha256sum "$D/$F" | cut -d' ' -f1)" = "$(cat "$D/$F.sha256")" ] && echo "sha256 match"
cp /root/komo_counts.py "$D/komo_counts.py"
cp /root/.komo-backup.pass "$D/pass"
docker run --rm --network none -v "$D:/w" node:22-bookworm bash -c '
set -e
cd /w
gpg --batch --pinentry-mode loopback --passphrase-file pass -d "'"$F"'" > komo.tar.gz
mkdir restored && tar -xzf komo.tar.gz -C restored
DB=$(ls restored/runtime/v3/d1/miniflare-D1DatabaseObject/*.sqlite | head -1)
python3 komo_counts.py "$DB" > restored.counts
rm -f pass
'
LIVE=$(ls $VOL/runtime/v3/d1/miniflare-D1DatabaseObject/*.sqlite | grep -v metadata)
python3 /root/komo_counts.py "$LIVE" | grep -v '^_cf' > "$D/live.counts"
grep -v '^_cf' "$D/restored.counts" > "$D/restored.f"
echo "--- restored (integrity + row counts)"; cat "$D/restored.f"
if diff <(grep -v -e rate_limits -e sessions -e oauth_states "$D/live.counts") <(grep -v -e rate_limits -e sessions -e oauth_states "$D/restored.f"); then echo "ROW COUNTS MATCH (excluding churning rate_limits/sessions/oauth_states)"; else echo "MISMATCH"; exit 1; fi
