# Evaluation Guide: aws-eks-cache-aware-llm-routing

## Purpose

This guide is for a reviewer who will independently verify the sample repository and blog claims by deploying the code, running benchmarks, and confirming results match documentation.

## Prerequisites

Before you begin, ensure you have:

- AWS account with **g5.2xlarge quota for 8 instances** in us-west-2
- AWS CLI v2 configured with appropriate permissions
- `kubectl`, `helm`, and `eksctl` installed
- A [HuggingFace token](https://huggingface.co/settings/tokens) with access to `mistralai/Mistral-7B-Instruct-v0.3`
- Budget: ~$20 (deployment runs for 2-3 hours at ~$8.25/hr in GPU costs)

## Step 1: Clone the Repository

```bash
git clone git@ssh.gitlab.aws.dev:achintan/aws-eks-cache-aware-llm-routing.git
cd aws-eks-cache-aware-llm-routing
```

## Step 2: Read the README

Read `README.md` end-to-end. Note down:
- The claimed benchmark results (69% p90 improvement, specific ms values per time bucket)
- The architecture flow described
- The security measures listed
- The deployment steps and time estimates

## Step 3: Review the Manifests

Open and review each file in `manifests/`:

1. **`cluster.yaml`** — Verify: EKS version, privateNetworking, clusterEndpoints config, secretsEncryption, cloudWatch logging
2. **`vllm-deployment.yaml`** — Verify: image tag pinned, securityContext present, resource limits set, ZMQ endpoint uses `$(POD_IP)` not `*`, NetworkPolicy restricts port 5556
3. **`llm-d-router-values.yaml`** — Verify: `secure-serving: true`, `metrics-endpoint-auth: true`, precise-prefix-cache-producer plugin config, blockSize matches vLLM
4. **`gateway.yaml`** — Verify: GatewayClass and Gateway defined
5. **`benchmark-runner.yaml`** — Verify: resource limits, securityContext

Review `scripts/setup.sh` — confirm no inline command substitution inside `kubectl exec`, uses `set -euo pipefail`, requires `KMS_KEY_ARN`.

## Step 4: Deploy

Follow the **Step-by-Step Deployment** section in README.md exactly as written. Do not use `scripts/setup.sh` on first pass — follow the manual steps to verify each command works individually.

```bash
export AWS_REGION=us-west-2
export HF_TOKEN=<your-token>
```

After each step, verify:
- After cluster creation: `kubectl get nodes` shows 8 Ready nodes
- After vLLM deploy: `kubectl -n inference get pods -l app=vllm-inference` shows 8/8 Running
- After Gateway install: `kubectl -n inference get gateway` shows Programmed=True
- After llm-d Router: `kubectl -n inference get pods -l llm-d-router-gateway` shows 2/2 Running

## Step 5: Verify Routing is Working

From the benchmark-runner pod, confirm both endpoints respond:

```bash
kubectl -n inference exec benchmark-runner -- python3 -c "
import urllib.request, json
for name, ep in [('RR', 'http://vllm-inference.inference.svc.cluster.local:8000/v1/completions'), ('CA', 'http://<envoy-gateway-svc>.envoy-gateway-system.svc.cluster.local:8080/v1/completions')]:
    req = urllib.request.Request(ep, data=json.dumps({'model':'mistralai/Mistral-7B-Instruct-v0.3','prompt':'Hello','max_tokens':5}).encode(), headers={'Content-Type':'application/json'})
    resp = urllib.request.urlopen(req, timeout=30)
    print(f'{name}: {resp.status}')
"
```

Both should return 200.

To find the envoy gateway service name:
```bash
kubectl -n envoy-gateway-system get svc -l gateway.networking.k8s.io/owning-gateway-name=inference-gateway
```

## Step 6: Verify Precise Scorer is Active

Check EPP logs for confirmation:
```bash
kubectl -n inference logs deployment/$(kubectl -n inference get deploy -l llm-d-router-gateway -o jsonpath='{.items[0].metadata.name}') -c epp | grep "precise-prefix-cache-producer"
```

You should see the plugin loaded. Also check ZMQ connections:
```bash
kubectl -n inference logs deployment/$(kubectl -n inference get deploy -l llm-d-router-gateway -o jsonpath='{.items[0].metadata.name}') -c epp | grep "Connected subscriber socket"
```

You should see 8 connections (one per vLLM pod).

## Step 7: Run the Benchmark

```bash
kubectl -n inference exec benchmark-runner -- pip install aiohttp
kubectl cp benchmarks/sustained_benchmark.py inference/benchmark-runner:/tmp/bench.py
kubectl -n inference exec benchmark-runner -- python3 /tmp/bench.py
```

This takes ~12 minutes (5 min per routing path + cooldown).

## Step 8: Compare Results

Compare the output against the README claims:

| What to check | README claims | Your result |
|---------------|--------------|-------------|
| RR p90 at end of run | ~4,443ms | |
| CA p90 at end of run | ~1,370ms | |
| Peak p90 improvement | 69% | |
| RR degrades over time | Yes (649ms → 4,443ms) | |
| CA stays more stable | Yes (288ms → 1,370ms) | |

> **Expected variance**: ±30% on absolute numbers is normal due to model warmth, GPU scheduling, and network jitter. What matters is: (a) RR clearly degrades over time, (b) CA degrades significantly less, (c) improvement is sustained across the run.

## Step 9: Verify Architecture Diagram

Compare `images/architecture-diagram.png` against what you actually deployed. Confirm:
- Client → ALB → Envoy Gateway → EPP → vLLM flow is accurate
- Port numbers match (8080, 9002, 8000, 5556)
- Component names match actual pod/service names

## Step 10: Cleanup

```bash
./scripts/cleanup.sh
```

Or manually:
```bash
eksctl delete cluster --name cache-routing-benchmark --region us-west-2
```

Verify in AWS Console: no orphaned EC2 instances, EBS volumes, or load balancers.

## Step 11: (Optional) Test setup.sh

If time permits, delete the cluster and re-deploy using the automated script:

```bash
export HF_TOKEN=<your-token>
export KMS_KEY_ARN=<your-kms-key-arn>
./scripts/setup.sh
```

Confirm it completes without manual intervention and endpoints work.

## What to Report

After testing, note:
1. Any command that didn't work as documented
2. Any time estimate that was significantly off
3. Whether benchmark results match the claimed pattern
4. Any security issues not addressed
5. Missing instructions or unclear steps
6. Any resources left behind after cleanup
