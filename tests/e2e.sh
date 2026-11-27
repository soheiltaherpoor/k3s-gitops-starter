#!/usr/bin/env bash
# End-to-end test of one environment: create a k3d cluster, install Flux, apply
# clusters/<env>, and check that the cluster converges on what the overlays say
# and puts back a manual change.
#
#   tests/e2e.sh staging|production
#
# Optional environment:
#   GIT_URL     repository Flux should clone (default: the URL in sync.yaml)
#   GIT_BRANCH  branch to clone
#   GIT_COMMIT  commit to check out; the test waits until Flux has applied it
#   KEEP_CLUSTER=1  leave the cluster running afterwards
set -euo pipefail

cd "$(dirname "$0")/.."

ENV_NAME=${1:-}
case "$ENV_NAME" in
  staging|production) ;;
  *) echo "usage: tests/e2e.sh staging|production" >&2; exit 2 ;;
esac

APP=podinfo
CLUSTER="gitops-$ENV_NAME"
PORT=19898
tmp=$(mktemp -d)
pf_pid=""

for tool in k3d kubectl flux age-keygen python3 curl; do
  command -v "$tool" >/dev/null || { echo "missing tool: $tool" >&2; exit 2; }
done

diagnostics() {
  echo "--- diagnostics ---" >&2
  kubectl get gitrepository,kustomization -A >&2 || true
  kubectl -n flux-system describe gitrepository,kustomization >&2 || true
  kubectl get pods -A >&2 || true
  kubectl -n flux-system logs deploy/kustomize-controller --tail=80 >&2 || true
  kubectl -n flux-system logs deploy/source-controller --tail=40 >&2 || true
  kubectl -n podinfo describe deploy,rs,pod >&2 || true
  kubectl -n podinfo get events >&2 || true
}

cleanup() {
  local rc=$?
  if [[ -n "$pf_pid" ]]; then kill "$pf_pid" 2>/dev/null || true; fi
  if (( rc != 0 )); then diagnostics; fi
  if [[ "${KEEP_CLUSTER:-0}" != "1" ]]; then
    k3d cluster delete "$CLUSTER" >/dev/null 2>&1 || true
  fi
  rm -rf "$tmp"
  exit "$rc"
}
trap cleanup EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }

# Poll until a jsonpath on a flux-system object contains the wanted text.
wait_for_revision() {
  local kind=$1 name=$2 path=$3 want=$4 deadline=$((SECONDS + 240)) got=""
  until got=$(kubectl -n flux-system get "$kind" "$name" -o "jsonpath=$path" 2>/dev/null) \
        && [[ "$got" == *"$want"* ]]; do
    (( SECONDS > deadline )) && fail "$kind/$name never reached revision $want (now: ${got:-none})"
    sleep 3
  done
}

echo "== repository invariants"
python3 tools/gitops.py check

want_tag=$(python3 tools/gitops.py show "$ENV_NAME" --app "$APP" --field tag)
want_replicas=$(python3 tools/gitops.py show "$ENV_NAME" --app "$APP" --field replicas)

echo "== cluster $CLUSTER"
k3d cluster create "$CLUSTER" --wait --timeout 240s \
  --k3s-arg "--disable=traefik@server:0" --k3s-arg "--disable=servicelb@server:0"

echo "== flux"
flux install --components=source-controller,kustomize-controller
kubectl -n flux-system wait deploy --all --for=condition=Available --timeout=180s

# The sync config asks for a decryption key. A throwaway one is enough here:
# nothing in the repository is encrypted, and no private key is ever committed.
age-keygen -o "$tmp/age.agekey" >/dev/null 2>&1
kubectl -n flux-system create secret generic sops-age --from-file=age.agekey="$tmp/age.agekey"

kubectl apply -k "clusters/$ENV_NAME"

if [[ -n "${GIT_URL:-}${GIT_BRANCH:-}${GIT_COMMIT:-}" ]]; then
  patch=$(GIT_URL="${GIT_URL:-}" GIT_BRANCH="${GIT_BRANCH:-}" GIT_COMMIT="${GIT_COMMIT:-}" python3 - <<'PY'
import json
import os

spec = {}
if os.environ["GIT_URL"]:
    spec["url"] = os.environ["GIT_URL"]
ref = {key: os.environ[var] for key, var in (("branch", "GIT_BRANCH"), ("commit", "GIT_COMMIT"))
       if os.environ[var]}
if ref:
    spec["ref"] = ref
print(json.dumps({"spec": spec}))
PY
)
  kubectl -n flux-system patch gitrepository platform --type merge -p "$patch"
fi

echo "== waiting for Flux to converge"
if [[ -n "${GIT_COMMIT:-}" ]]; then
  # Without this, a Ready condition left over from the default branch could pass.
  wait_for_revision gitrepository platform '{.status.artifact.revision}' "$GIT_COMMIT"
  wait_for_revision kustomization infrastructure '{.status.lastAppliedRevision}' "$GIT_COMMIT"
  wait_for_revision kustomization apps '{.status.lastAppliedRevision}' "$GIT_COMMIT"
fi
kubectl -n flux-system wait gitrepository/platform --for=condition=Ready --timeout=300s
kubectl -n flux-system wait kustomization/infrastructure --for=condition=Ready --timeout=300s
kubectl -n flux-system wait kustomization/apps --for=condition=Ready --timeout=420s
kubectl -n podinfo rollout status "deploy/$APP" --timeout=180s

echo "== what is running matches the $ENV_NAME overlay"
image=$(kubectl -n podinfo get "deploy/$APP" -o jsonpath='{.spec.template.spec.containers[0].image}')
[[ "$image" == *":$want_tag" ]] || fail "image is $image, overlay says tag $want_tag"

replicas=$(kubectl -n podinfo get "deploy/$APP" -o jsonpath='{.spec.replicas}')
[[ "$replicas" == "$want_replicas" ]] || fail "replicas are $replicas, overlay says $want_replicas"

psa=$(kubectl get ns podinfo -o 'jsonpath={.metadata.labels.pod-security\.kubernetes\.io/enforce}')
[[ "$psa" == "restricted" ]] || fail "namespace Pod Security level is '$psa', expected restricted"

kubectl -n podinfo get resourcequota podinfo >/dev/null || fail "resource quota missing"

if kubectl -n podinfo get pdb podinfo >/dev/null 2>&1; then have_pdb=yes; else have_pdb=no; fi
if [[ "$ENV_NAME" == "production" ]]; then
  [[ "$have_pdb" == "yes" ]] || fail "production should have a PodDisruptionBudget"
else
  [[ "$have_pdb" == "no" ]] || fail "staging should not have a PodDisruptionBudget"
fi

echo "== the app itself reports the promoted version"
kubectl -n podinfo port-forward "svc/$APP" "$PORT:9898" >"$tmp/port-forward.log" 2>&1 &
pf_pid=$!
body=""
for _ in $(seq 1 30); do
  if body=$(curl -fsS "http://127.0.0.1:$PORT/version" 2>/dev/null); then break; fi
  body=""
  sleep 1
done
[[ -n "$body" ]] || fail "no answer from /version through the port-forward"
reported=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["version"])' <<<"$body")
[[ "$reported" == "$want_tag" ]] || fail "app reports version $reported, overlay says $want_tag"

echo "== a manual change is put back"
kubectl -n podinfo scale "deploy/$APP" --replicas=3
flux reconcile kustomization apps --with-source
deadline=$((SECONDS + 120))
until [[ "$(kubectl -n podinfo get "deploy/$APP" -o jsonpath='{.spec.replicas}')" == "$want_replicas" ]]; do
  (( SECONDS > deadline )) && fail "replicas were not restored to $want_replicas"
  sleep 3
done

echo "ok: $ENV_NAME converged on tag $want_tag with $want_replicas replica(s) and corrected drift"
