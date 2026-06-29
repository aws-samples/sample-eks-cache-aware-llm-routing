#!/bin/bash
# End-to-end deployment script for cache-aware LLM routing on Amazon EKS
# Prerequisites: aws cli, kubectl, helm, eksctl, HF_TOKEN env var set

set -euo pipefail

REGION=${AWS_REGION:-us-west-2}
CLUSTER_NAME=${CLUSTER_NAME:-cache-routing-benchmark}
NAMESPACE=inference
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "=== Cache-Aware LLM Routing on EKS - Setup ==="
echo "Region: $REGION"
echo "Cluster: $CLUSTER_NAME"
echo ""

# Validate prerequisites
if [ -z "${HF_TOKEN:-}" ]; then
  echo "ERROR: HF_TOKEN environment variable not set."
  echo "Get a token from https://huggingface.co/settings/tokens"
  exit 1
fi

if [ -z "${KMS_KEY_ARN:-}" ]; then
  echo "ERROR: KMS_KEY_ARN environment variable not set."
  echo "Create a KMS key: aws kms create-key --region $REGION"
  exit 1
fi

command -v eksctl >/dev/null || { echo "ERROR: eksctl not found"; exit 1; }
command -v kubectl >/dev/null || { echo "ERROR: kubectl not found"; exit 1; }
command -v helm >/dev/null || { echo "ERROR: helm not found"; exit 1; }

# Step 1: Create EKS cluster
echo ">>> Step 1: Creating EKS cluster (~15 min)..."
sed "s|\${KMS_KEY_ARN}|${KMS_KEY_ARN}|g" "$SCRIPT_DIR/manifests/cluster.yaml" | eksctl create cluster -f -
echo "Cluster created."

# Step 2: Install NVIDIA device plugin
echo ">>> Step 2: Installing NVIDIA device plugin..."
kubectl apply -f https://raw.githubusercontent.com/NVIDIA/k8s-device-plugin/v0.14.5/nvidia-device-plugin.yml
echo "NVIDIA plugin installed."

# Step 3: Create namespace and secrets
echo ">>> Step 3: Creating namespace and secrets..."
kubectl create namespace $NAMESPACE --dry-run=client -o yaml | kubectl apply -f -
kubectl -n $NAMESPACE create secret generic hf-token --from-literal=token="$HF_TOKEN" --dry-run=client -o yaml | kubectl apply -f -
kubectl -n $NAMESPACE create secret generic llm-d-hf-token --from-literal=HF_TOKEN="$HF_TOKEN" --dry-run=client -o yaml | kubectl apply -f -
echo "Secrets created."

# Step 4: Deploy vLLM
echo ">>> Step 4: Deploying vLLM (8 replicas, ~5 min for model loading)..."
kubectl apply -f "$SCRIPT_DIR/manifests/vllm-deployment.yaml"
echo "Waiting for vLLM pods to be ready..."
kubectl -n $NAMESPACE wait --for=condition=Ready pod -l app=vllm-inference --timeout=600s
echo "vLLM ready."

# Step 5: Install Gateway API CRDs + Envoy Gateway
echo ">>> Step 5: Installing Gateway API and Envoy Gateway..."
kubectl apply -f https://github.com/kubernetes-sigs/gateway-api-inference-extension/releases/download/v1.5.0/install.yaml
helm install eg oci://docker.io/envoyproxy/gateway-helm --version v1.2.0 \
  -n envoy-gateway-system --create-namespace --wait
kubectl apply -f "$SCRIPT_DIR/manifests/gateway.yaml"
echo "Gateway installed."

# Step 6: Deploy llm-d Router with precise scorer
echo ">>> Step 6: Deploying llm-d Router (precise prefix-cache scorer)..."
helm install cache-aware-routing oci://ghcr.io/llm-d/charts/llm-d-router-gateway-dev \
  --version v0 -n $NAMESPACE \
  -f "$SCRIPT_DIR/manifests/llm-d-router-values.yaml"
echo "Waiting for EPP to be ready..."
sleep 30
kubectl -n $NAMESPACE wait --for=condition=Ready pod -l llm-d-router-gateway=cache-aware-routing-epp --timeout=120s
echo "llm-d Router ready."

# Step 7: Deploy benchmark runner
echo ">>> Step 7: Deploying benchmark runner..."
kubectl apply -f "$SCRIPT_DIR/manifests/benchmark-runner.yaml"
kubectl -n $NAMESPACE wait --for=condition=Ready pod/benchmark-runner --timeout=120s
kubectl -n $NAMESPACE exec benchmark-runner -- pip install aiohttp --quiet
echo "Benchmark runner ready."

# Step 8: Verify endpoints
echo ">>> Step 8: Verifying endpoints..."
CA_SVC=$(kubectl -n envoy-gateway-system get svc \
  -l gateway.networking.k8s.io/owning-gateway-name=inference-gateway \
  -o jsonpath='{.items[0].metadata.name}')

kubectl -n $NAMESPACE exec benchmark-runner -- python3 -c "
import urllib.request, json
endpoints = {
    'Round-Robin': 'http://vllm-inference.inference.svc.cluster.local:8000/v1/completions',
    'Cache-Aware': 'http://${CA_SVC}.envoy-gateway-system.svc.cluster.local:8080/v1/completions'
}
for name, ep in endpoints.items():
    req = urllib.request.Request(ep, data=json.dumps({'model':'mistralai/Mistral-7B-Instruct-v0.3','prompt':'Hello','max_tokens':5}).encode(), headers={'Content-Type':'application/json'})
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        print(f'{name}: OK')
    except Exception as e:
        print(f'{name}: FAIL - {e}')
"

echo ""
echo "=== Setup Complete ==="
echo "Run the benchmark with:"
echo "  kubectl cp benchmarks/sustained_benchmark.py $NAMESPACE/benchmark-runner:/tmp/bench.py"
echo "  kubectl -n $NAMESPACE exec benchmark-runner -- python3 /tmp/bench.py"
