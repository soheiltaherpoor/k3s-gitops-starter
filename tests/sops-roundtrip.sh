#!/usr/bin/env bash
# Proves the secret workflow with a throwaway age key, in a scratch copy of the
# repository: a plaintext Secret is rejected, the same file encrypted with sops
# is accepted, and only the key holder can read it back.
set -euo pipefail

cd "$(dirname "$0")/.."

for tool in age-keygen sops python3; do
  command -v "$tool" >/dev/null || { echo "missing tool: $tool" >&2; exit 2; }
done

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

scratch="$tmp/repo"
mkdir "$scratch"
tar --exclude=.git --exclude=__pycache__ --exclude=.build -cf - . | tar -xf - -C "$scratch"

age-keygen -o "$tmp/key.txt" >/dev/null 2>&1
recipient=$(age-keygen -y "$tmp/key.txt")

# sops finds .sops.yaml from the working directory (the real repository, with its
# placeholder recipient). Point the scratch copy's rule at the throwaway key and
# hand that file to sops explicitly.
sed -i.bak "s/age1[a-z0-9]*\$/${recipient}/" "$scratch/.sops.yaml"

secret="$scratch/apps/podinfo/overlays/staging/db.sops.yaml"
cat > "$secret" <<'EOF'
apiVersion: v1
kind: Secret
metadata:
  name: db
  namespace: podinfo
type: Opaque
stringData:
  password: roundtrip-demo-value
EOF

echo "== plaintext Secret must be rejected"
if python3 tools/gitops.py --root "$scratch" check >"$tmp/plain.out" 2>&1; then
  cat "$tmp/plain.out"
  echo "FAIL: the checker accepted a plaintext Secret" >&2
  exit 1
fi
grep -q "db.sops.yaml" "$tmp/plain.out" || { cat "$tmp/plain.out"; echo "FAIL: wrong complaint" >&2; exit 1; }

echo "== encrypt with sops"
sops --config "$scratch/.sops.yaml" --encrypt --age "$recipient" --encrypted-regex '^(data|stringData)$' --in-place "$secret"
if grep -q "roundtrip-demo-value" "$secret"; then
  echo "FAIL: the plaintext value is still in the encrypted file" >&2
  exit 1
fi
grep -q "ENC\[" "$secret" || { echo "FAIL: no encrypted values in the file" >&2; exit 1; }

echo "== encrypted Secret must be accepted"
python3 tools/gitops.py --root "$scratch" check

echo "== the key holder can read it back, nobody else can"
decrypted=$(SOPS_AGE_KEY_FILE="$tmp/key.txt" sops --decrypt "$secret")
grep -q "roundtrip-demo-value" <<<"$decrypted" || { echo "FAIL: decryption lost the value" >&2; exit 1; }

age-keygen -o "$tmp/other.txt" >/dev/null 2>&1
if SOPS_AGE_KEY_FILE="$tmp/other.txt" sops --decrypt "$secret" >/dev/null 2>&1; then
  echo "FAIL: a different key decrypted the file" >&2
  exit 1
fi

echo "ok: plaintext rejected, encrypted accepted, decryption needs the key"
