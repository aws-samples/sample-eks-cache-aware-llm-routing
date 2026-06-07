# Cache-Aware LLM Routing on Amazon EKS with Gateway API Inference Extension and llm-d

This sample demonstrates how to deploy precise KV-cache-aware inference routing on Amazon EKS using the [Kubernetes Gateway API Inference Extension](https://gateway-api-inference-extension.sigs.k8s.io/) and [llm-d](https://llm-d.ai/), reducing tail latency (p90 TTFT) by up to 69% compared to standard round-robin routing under sustained multi-turn load.

## Overview

When serving LLMs at scale with multiple replicas, standard Kubernetes load balancing scatters requests across pods without awareness of GPU KV-cache state. This forces each pod to recompute the KV-cache for shared prompt prefixes from scratch — wasting GPU cycles and increasing time-to-first-token (TTFT).

Cache-aware routing solves this by maintaining a real-time global index of which KV-cache blocks reside on which pod, and routing each request to the pod with the highest prefix-cache affinity.

### Architecture

```
Client
  → Application Load Balancer (external ingress)
    → Envoy Gateway (port 8080)
      → ext-proc gRPC → llm-d EPP (Endpoint Picker)
        EPP scores pods: precise-prefix-cache(3) + queue(2) + kv-util(2)
      ← routing decision (selected pod IP)
    → Envoy forwards to chosen vLLM pod (port 8000)
      → vLLM processes with prefix caching + KVEvents publishing
  ← streaming response
```

### How It Works

1. **vLLM pods publish KV-cache events** over ZeroMQ on every block allocation/eviction
2. **llm-d EPP subscribes per pod** via pod discovery, building a global prefix-block index
3. **On each request**, the EPP tokenizes the prompt, looks up which pods hold matching blocks, and routes to the pod with the highest cache hit fraction

## Benchmark Results

**Configuration**: 150 concurrent users, 25 QPS (Poisson arrival), 8× vLLM pods (Mistral-7B on g5.2xlarge), 3-minute sustained multi-turn load.

| Time Bucket | Round-Robin p90 | Cache-Aware p90 | Improvement |
|-------------|-----------------|-----------------|-------------|
| 0-30s       | 649ms           | 288ms           | **+56%**    |
| 30-60s      | 1,276ms         | 711ms           | **+44%**    |
| 60-90s      | 2,321ms         | 1,048ms         | **+55%**    |
| 90-120s     | 2,571ms         | 1,091ms         | **+58%**    |
| 150-180s    | 4,443ms         | 1,370ms         | **+69%**    |

Round-robin TTFT degrades to 4.4 seconds under sustained load while cache-aware routing holds at 1.4 seconds.

## Prerequisites

- AWS account with permissions for EKS, EC2 (GPU instances), and ELB
- [AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) configured
- [kubectl](https://kubernetes.io/docs/tasks/tools/) v1.30+
- [Helm](https://helm.sh/docs/intro/install/) v3.12+
- [eksctl](https://eksctl.io/installation/) v0.170+
- A [Hugging Face token](https://huggingface.co/settings/tokens) with access to `mistralai/Mistral-7B-Instruct-v0.3`
- Service quota for 8× `g5.2xlarge` instances in your target region

## Cost

⚠️ **This sample provisions GPU instances that incur significant cost.**

| Resource | Instance Type | Quantity | Estimated Cost |
|----------|--------------|----------|----------------|
| EKS Cluster | - | 1 | $0.10/hr |
| GPU Nodes | g5.2xlarge | 8 | $8.08/hr ($1.01 each) |
| NAT Gateway | - | 1 | $0.045/hr |
| ALB | - | 1 | $0.023/hr |
| **Total** | | | **~$8.25/hr** |

Use the cleanup script to tear down resources when done. The benchmark itself takes ~15 minutes to run.

## Quick Start

### 1. Create EKS Cluster

```bash
export AWS_REGION=us-west-2
export CLUSTER_NAME=cache-routing-benchmark

eksctl create cluster -f manifests/cluster.yaml
```

### 2. Deploy vLLM with KVEvents

```bash
# Create HuggingFace token secret
kubectl -n inference create secret generic hf-token \
  --from-literal=token=$HF_TOKEN

# Deploy vLLM (8 replicas with prefix caching + ZMQ KVEvents)
kubectl apply -f manifests/vllm-deployment.yaml
```

### 3. Install Gateway API and Envoy Gateway

```bash
# Gateway API CRDs
kubectl apply -f https://github.com/kubernetes-sigs/gateway-api-inference-extension/releases/download/v1.5.0/install.yaml

# Envoy Gateway
helm install eg oci://docker.io/envoyproxy/gateway-helm \
  --version v1.2.0 -n envoy-gateway-system --create-namespace

# Gateway and GatewayClass
kubectl apply -f manifests/gateway.yaml
```

### 4. Deploy llm-d Router (Precise Scorer)

```bash
# Create HF token for tokenizer sidecar
kubectl -n inference create secret generic llm-d-hf-token \
  --from-literal=HF_TOKEN=$HF_TOKEN

# Deploy via Helm
helm install cache-aware-routing \
  oci://ghcr.io/llm-d/charts/llm-d-router-gateway-dev \
  --version v0 -n inference \
  -f manifests/llm-d-router-values.yaml
```

### 5. Run Benchmark

```bash
# Wait for all pods to be ready
kubectl -n inference wait --for=condition=Ready pod -l app=vllm-inference --timeout=600s

# Deploy benchmark runner
kubectl apply -f manifests/benchmark-runner.yaml

# Install dependencies
kubectl -n inference exec benchmark-runner -- pip install aiohttp --quiet

# Copy and run benchmark
kubectl cp benchmarks/sustained_benchmark.py inference/benchmark-runner:/tmp/bench.py
kubectl -n inference exec benchmark-runner -- python3 /tmp/bench.py
```

### 6. Cleanup

```bash
# Delete cluster (stops all billing)
eksctl delete cluster --name $CLUSTER_NAME --region $AWS_REGION
```

## Repository Structure

```
.
├── README.md                              # This file
├── LICENSE                                # MIT-0
├── CONTRIBUTING.md                        # Contribution guidelines
├── CODE_OF_CONDUCT.md                     # Code of conduct
├── manifests/
│   ├── cluster.yaml                       # eksctl cluster definition
│   ├── vllm-deployment.yaml              # vLLM with KVEvents + prefix caching
│   ├── gateway.yaml                       # Envoy Gateway + GatewayClass
│   ├── llm-d-router-values.yaml          # Helm values for precise scorer
│   └── benchmark-runner.yaml             # Benchmark pod
├── benchmarks/
│   └── sustained_benchmark.py            # Multi-turn sustained load benchmark
├── scripts/
│   ├── setup.sh                          # End-to-end deployment script
│   └── cleanup.sh                        # Tear down all resources
├── docs/
│   └── architecture.md                   # Detailed architecture explanation
└── images/
    └── architecture-diagram.png          # Architecture diagram
```

## Key Configuration Details

### vLLM Args (for KVEvents)

```yaml
args:
  - --enable-prefix-caching
  - --prefix-caching-hash-algo sha256_cbor
  - --block-size 64
  - --kv-events-config '{"enable_kv_cache_events":true,"publisher":"zmq","endpoint":"tcp://*:5556","topic":"kv@$(POD_IP):8000@mistralai/Mistral-7B-Instruct-v0.3"}'
env:
  - name: PYTHONHASHSEED
    value: "42"
```

### llm-d Scorer Plugins

```yaml
plugins:
  - type: token-producer
  - type: endpoint-notification-source
  - type: precise-prefix-cache-producer
    parameters:
      tokenProcessorConfig:
        blockSize: 64           # Must match vLLM --block-size
      kvEventsConfig:
        discoverPods: true
        podDiscoveryConfig:
          socketPort: 5556      # vLLM ZMQ port
  - type: prefix-cache-scorer
    parameters:
      prefixMatchInfoProducerName: precise-prefix-cache-producer
  - type: kv-cache-utilization-scorer
  - type: queue-scorer
```

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.

## Related Resources

- [llm-d Documentation](https://llm-d.ai/docs/getting-started)
- [Precise Prefix Cache Routing Guide](https://github.com/llm-d/llm-d/blob/main/guides/precise-prefix-cache-routing/README.md)
- [Gateway API Inference Extension](https://gateway-api-inference-extension.sigs.k8s.io/)
- [vLLM Automatic Prefix Caching](https://docs.vllm.ai/en/latest/features/automatic_prefix_caching.html)
- [Amazon EKS Documentation](https://docs.aws.amazon.com/eks/latest/userguide/)
