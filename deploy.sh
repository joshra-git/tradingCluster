#!/usr/bin/env bash
set -euo pipefail

CLUSTER_TOOL="${CLUSTER_TOOL:-kind}"   # kind | k3d
CLUSTER_NAME="${CLUSTER_NAME:-trading}"

echo "==> Building images"
docker build -t trading-controller:latest ./controller
docker build -t trading-agent:latest ./agent

echo "==> Loading images into the local cluster ($CLUSTER_TOOL)"
if [ "$CLUSTER_TOOL" = "kind" ]; then
  kind load docker-image trading-controller:latest --name "$CLUSTER_NAME"
  kind load docker-image trading-agent:latest --name "$CLUSTER_NAME"
else
  k3d image import trading-controller:latest trading-agent:latest -c "$CLUSTER_NAME"
fi

echo "==> Injecting db/schema.sql into the Postgres init ConfigMap"
python3 - <<'PY'
import re
schema = open("db/schema.sql").read()
indented = "\n".join("    " + line for line in schema.splitlines())
manifest = open("k8s/03-postgres.yaml").read()
manifest = re.sub(
    r"(schema\.sql: \|\n).*?(\n---)",
    lambda m: m.group(1) + indented + m.group(2),
    manifest, count=1, flags=re.DOTALL,
)
open("k8s/03-postgres.yaml", "w").write(manifest)
PY

if [ ! -f k8s/01-secrets.yaml ]; then
  echo "!! k8s/01-secrets.yaml not found."
  echo "!! Copy k8s/01-secrets.example.yaml to k8s/01-secrets.yaml and fill in your real keys first."
  exit 1
fi

echo "==> Applying manifests"
kubectl apply -f k8s/00-namespace.yaml
kubectl apply -f k8s/01-secrets.yaml
kubectl apply -f k8s/02-configmap-tiers.yaml
kubectl apply -f k8s/03-postgres.yaml
kubectl apply -f k8s/04-controller-rbac.yaml
kubectl apply -f k8s/05-controller-deployment.yaml
kubectl apply -f k8s/06-networkpolicies.yaml

echo "==> Waiting for controller to be ready"
kubectl -n trading rollout status deployment/controller --timeout=120s

echo "==> Done. Check logs with: kubectl -n trading logs -f deploy/controller"
