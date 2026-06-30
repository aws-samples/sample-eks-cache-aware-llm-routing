# Evaluator Prompt: AWS Sample Validation — aws-eks-cache-aware-llm-routing

## Your Role

You are a technical evaluator tasked with end-to-end verification of an AWS sample repository and its associated blog claims. Your job is to:

1. **Deploy** the sample from scratch following only the instructions in the repo
2. **Validate** that every claim, command, and result in the README is accurate and reproducible
3. **Run the benchmark** and verify the performance improvement claims
4. **Report** any discrepancies, broken steps, missing instructions, or inaccurate claims

## What You Are Validating

A blog post titled "Cache-aware LLM routing on Amazon EKS using Gateway API Inference Extension and llm-d" claims:
- Precise KV-cache-aware routing reduces p90 TTFT by **69%** under sustained multi-turn load
- Round-robin degrades to **4.4 seconds** while cache-aware holds at **1.4 seconds**
- Setup takes **~25 minutes** end-to-end
- 150 concurrent users, 25 QPS Poisson arrival, 8× vLLM pods on g5.2xlarge

## Repository Location

```
git clone git@ssh.gitlab.aws.dev:achintan/aws-eks-cache-aware-llm-routing.git
```

## Files to Review and Validate

### Core (must read before deploying):
| File | Purpose | Validate |
|------|---------|----------|
| `README.md` | Main documentation, all claims, deployment steps | Every command works, time estimates are reasonable, benchmark numbers match |
| `manifests/cluster.yaml` | eksctl cluster definition | Creates successfully, private networking enabled, KMS placeholder, audit logging |
| `manifests/vllm-deployment.yaml` | vLLM + NetworkPolicy | 8 pods start, KVEvents working (check ZMQ on port 5556), security context applied |
| `manifests/gateway.yaml` | Envoy Gateway + GatewayClass | Gateway becomes Programmed=True, ALB provisioned |
| `manifests/llm-d-router-values.yaml` | Helm values for precise scorer | EPP starts 2/2, precise-prefix-cache-producer loads, ZMQ subscribers connect |
| `manifests/benchmark-runner.yaml` | Benchmark pod | Starts, pip install works, can reach both endpoints |
| `benchmarks/sustained_benchmark.py` | Benchmark script | Runs without error, produces valid TTFT measurements |
| `scripts/setup.sh` | Automated deployment | Runs end-to-end without manual intervention (given env vars) |
| `scripts/cleanup.sh` | Teardown | Deletes cluster completely |

### Supporting (review for correctness):
| File | Purpose |
|------|---------|
| `LICENSE` | MIT-0 (standard aws-samples) |
| `CONTRIBUTING.md` | Standard contribution guidelines |
| `CODE_OF_CONDUCT.md` | Amazon open source CoC |
| `images/architecture-diagram.png` | Architecture diagram matches actual deployment |
| `images/benchmark-results.png` | Chart matches claimed numbers |
| `architecture.drawio` | Editable source for diagram |

## Validation Checklist

### Phase 1: Code Review (before deploying)

- [ ] README claims match code (no phantom features, no missing steps)
- [ ] Security: no secrets hardcoded, no credentials in manifests
- [ ] Image tags are pinned (not `:latest`)
- [ ] Resource requests/limits are set on all containers
- [ ] SecurityContext is applied (allowPrivilegeEscalation: false, drop ALL)
- [ ] NetworkPolicy is present and correctly scoped
- [ ] cluster.yaml has: privateNetworking, clusterEndpoints, secretsEncryption, cloudWatch logging
- [ ] llm-d-router-values.yaml has: secure-serving=true, metrics-endpoint-auth=true
- [ ] vllm-deployment.yaml: ZMQ binds to $(POD_IP) not *, PYTHONHASHSEED=42, block-size=64
- [ ] setup.sh uses `set -euo pipefail`, no inline command substitution in kubectl exec
- [ ] Blog outline claims (in docs/blog-outline.md) match README results

### Phase 2: Deployment (follow README or setup.sh)

Prerequisites you need:
- AWS account with g5.2xlarge quota (8 instances) in us-west-2
- HuggingFace token with access to `mistralai/Mistral-7B-Instruct-v0.3`
- KMS key ARN (create one: `aws kms create-key --region us-west-2`)

Deploy and verify:
- [ ] `eksctl create cluster -f manifests/cluster.yaml` succeeds
- [ ] 8 nodes become Ready
- [ ] NVIDIA device plugin installed, GPUs allocatable
- [ ] vLLM 8/8 pods Running, readiness probes passing
- [ ] Check vLLM logs: `kv_events_config` shows `enable_kv_cache_events=True`, ZMQ on pod IP
- [ ] Envoy Gateway installed, Gateway becomes `Programmed=True`
- [ ] Helm install of llm-d router succeeds, EPP pod is 2/2 (epp + vllm-render sidecar)
- [ ] EPP logs show: `precise-prefix-cache-producer` loaded, ZMQ subscribers connected to vLLM pods
- [ ] NetworkPolicy created (`restrict-zmq-kv-events`)
- [ ] benchmark-runner pod Running, `pip install aiohttp` works

### Phase 3: Endpoint Verification

- [ ] Round-Robin: `curl http://vllm-inference.inference.svc.cluster.local:8000/v1/completions` returns 200
- [ ] Cache-Aware: `curl http://<envoy-gateway-svc>.envoy-gateway-system.svc.cluster.local:8080/v1/completions` returns 200
- [ ] Send two requests with same prefix to CA endpoint → EPP logs show same pod selected (cache affinity working)

### Phase 4: Benchmark Validation

Run the benchmark:
```bash
kubectl cp benchmarks/sustained_benchmark.py inference/benchmark-runner:/tmp/bench.py
kubectl -n inference exec benchmark-runner -- python3 /tmp/bench.py
```

Validate:
- [ ] Script runs without errors
- [ ] Both RR and CA paths complete (no timeouts, minimal errors)
- [ ] Results show **consistent improvement** for CA over RR (any sustained improvement counts)
- [ ] The general pattern holds: RR degrades over time, CA degrades less
- [ ] Numbers should be in the same order of magnitude as claimed (±30% acceptable due to variance):
  - RR p90 should reach >2000ms by the end
  - CA p90 should stay <1500ms
  - Improvement should be >30% on p90

> **Note**: Exact numbers (69%, 4443ms, 1370ms) will vary between runs due to model loading state, cache warmth, and scheduling jitter. The pattern and order of magnitude matter more than exact reproduction.

### Phase 5: Cleanup

- [ ] `./scripts/cleanup.sh` or `eksctl delete cluster` removes all resources
- [ ] No orphaned resources (check EC2, ELB, EBS in console)

## Reporting Template

```markdown
## Evaluation Report: aws-eks-cache-aware-llm-routing

**Date**: 
**Evaluator**: 
**Region**: us-west-2
**Total deployment time**: 

### Code Review
- Pass/Fail: 
- Issues found: 

### Deployment
- Pass/Fail: 
- Time taken: 
- Issues encountered: 

### Endpoint Verification
- Round-Robin: Pass/Fail
- Cache-Aware: Pass/Fail
- Prefix affinity confirmed: Yes/No

### Benchmark Results
| Metric | Claimed | Actual | Status |
|--------|---------|--------|--------|
| RR p90 end | 4,443ms | ___ ms | Pass/Fail |
| CA p90 end | 1,370ms | ___ ms | Pass/Fail |
| Peak improvement | 69% | ___% | Pass/Fail |
| RR degrades over time | Yes | Yes/No | Pass/Fail |
| CA stays stable | Yes | Yes/No | Pass/Fail |

### Cleanup
- Pass/Fail: 
- Orphaned resources: 

### Overall Verdict: PASS / FAIL / PASS WITH NOTES

### Recommendations:
- 
```

## Important Notes for the Evaluator

1. **Cost**: This deployment costs ~$8.25/hr in GPU instances. Budget ~$20 for a full evaluation (2-3 hours including setup, benchmark, debugging).

2. **Known limitation**: vLLM container runs as root (CUDA requirement). This is documented and `allowPrivilegeEscalation: false` + `drop ALL capabilities` is applied as mitigation.

3. **Variance**: Benchmark results are statistical. Run at least once with the full 3-minute duration. If results don't show improvement, check that EPP logs show ZMQ subscribers connected to all 8 pods.

4. **Troubleshooting**: If CA endpoint returns 504, restart the EPP deployment and wait 30s for ZMQ reconnection. If vLLM pods are pending, check GPU quota and nodegroup status.
