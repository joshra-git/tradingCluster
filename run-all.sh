#!/usr/bin/env bash
set -euo pipefail

CLUSTER_NAME="trading"
NAMESPACE="trading"

echo "==> Checking for an existing '$CLUSTER_NAME' cluster"
if kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
  echo "    cluster already running, skipping creation"
else
  echo "==> Creating kind cluster from kind-config.yaml"
  kind create cluster --config kind-config.yaml
fi

echo "==> Building images"
docker build -t trading-controller:latest ./controller
docker build -t trading-agent:latest ./agent
docker build -t trading-dashboard:latest ./dashboard

echo "==> Loading images into the cluster"
kind load docker-image trading-controller:latest --name "$CLUSTER_NAME"
kind load docker-image trading-agent:latest --name "$CLUSTER_NAME"
kind load docker-image trading-dashboard:latest --name "$CLUSTER_NAME"

echo "==> Applying manifests (secrets deliberately NOT touched)"
kubectl apply -f k8s/00-namespace.yaml
# k8s/01-secrets.yaml is intentionally skipped - whatever is already in the
# cluster stays as-is. Nothing here reads, overwrites, or prints it.
if kubectl -n "$NAMESPACE" get secret trading-secrets >/dev/null 2>&1; then
  echo "    existing 'trading-secrets' found and left untouched"
else
  echo "!! No 'trading-secrets' secret exists in the cluster yet."
  echo "!! Apply it once by hand before the controller will start:"
  echo "     kubectl apply -f k8s/01-secrets.yaml"
  exit 1
fi
kubectl apply -f k8s/02-configmap-tiers.yaml
kubectl apply -f k8s/03-postgres.yaml
kubectl apply -f k8s/04-controller-rbac.yaml
kubectl apply -f k8s/05-controller-deployment.yaml
kubectl apply -f k8s/06-networkpolicies.yaml
kubectl apply -f k8s/07-dashboard.yaml

echo "==> Restarting workloads to pick up the new images"
kubectl -n "$NAMESPACE" rollout restart deployment/controller
kubectl -n "$NAMESPACE" rollout restart deployment/dashboard

# Agent deployments are created dynamically by the controller, so their names
# aren't known ahead of time - restart whatever is currently running by label.
AGENTS=$(kubectl -n "$NAMESPACE" get deployments -l app=trading-agent -o name 2>/dev/null || true)
if [ -n "$AGENTS" ]; then
  echo "$AGENTS" | while read -r dep; do
    echo "    restarting $dep"
    kubectl -n "$NAMESPACE" rollout restart "$dep"
  done
else
  echo "    no agent deployments running yet (the controller seeds them on startup)"
fi

echo "==> Waiting for the controller to come back"
kubectl -n "$NAMESPACE" rollout status deployment/controller --timeout=120s

echo "==> Done."
kubectl -n "$NAMESPACE" get pods
echo
echo "Useful next commands:"
echo "    kubectl -n $NAMESPACE logs -f deploy/controller"
echo "    kubectl -n $NAMESPACE logs -f -l app=trading-agent"
echo "    kubectl -n $NAMESPACE port-forward svc/dashboard 8091:8095"
