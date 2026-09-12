#!/usr/bin/env bash
set -euo pipefail

CLUSTER_NAME="trading"

echo "==> Checking for an existing '$CLUSTER_NAME' cluster"
if kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
  echo "    cluster already running, skipping creation"
else
  echo "==> Creating kind cluster from kind-config.yaml"
  kind create cluster --config kind-config.yaml
fi

echo "==> Handing off to deploy.sh for build + load + apply"
./deploy.sh

echo "==> Everything is up. Useful next commands:"
echo "    kubectl -n trading get pods"
echo "    kubectl -n trading logs -f deploy/controller"
