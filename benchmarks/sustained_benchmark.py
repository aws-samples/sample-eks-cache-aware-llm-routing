"""
sustained_benchmark.py — Sustained 5-min multi-turn benchmark with Poisson arrivals.

Methodology matched to llm-d "KV-Cache Wins You Can See" blog:
- 50 users with unique 6000-token system prompts (shared prefix structure)
- Poisson arrival pattern (lambda = target QPS)
- Users cycle through turns (context grows each turn)
- Time-series TTFT output bucketed every 30s
- Runs both RR and CA for 5 minutes each

Usage:
  kubectl -n inference exec benchmark-runner -- python3 /tmp/sustained_bench.py
"""
import asyncio, aiohttp, time, json, random, string, math

random.seed(42)

# === CONFIG ===
MODEL = 'mistralai/Mistral-7B-Instruct-v0.3'
RR = 'http://vllm-inference.inference.svc.cluster.local:8000/v1/completions'
CA = 'http://envoy-inference-inference-gateway-2e466d4a.envoy-gateway-system.svc.cluster.local:8080/v1/completions'

USERS = 50
DURATION_S = 300       # 5 minutes
BUCKET_S = 30          # time-series bucket width
TARGET_QPS = 10        # Poisson lambda (requests per second)
MAX_TOKENS = 64
MAX_CONCURRENCY = 50
TURN_CONTEXT_TOKENS = 200  # tokens added per turn per user

# ~4500 token system prompt (~12500 chars at ~2.8 chars/token for Mistral)
# Leaves room for per-user context growth within 8192 max_model_len
SYSTEM_PROMPT = (
    "You are a senior AWS Solutions Architect specializing in distributed systems, "
    "Kubernetes, and machine learning infrastructure. You help enterprise customers "
    "design highly available, cost-optimized architectures. "
) + ''.join(random.choices(string.ascii_lowercase + ' ', k=12000))

# Unique per-user context (~60 tokens each, grows per turn)
USER_CONTEXTS = [
    f"CustomerID:{i} Context:" + ''.join(random.choices(string.ascii_lowercase + ' ', k=150))
    for i in range(USERS)
]


def build_prompt(user_id: int, turn: int) -> str:
    """Build prompt with shared prefix + growing per-user context (capped at 10 turns)."""
    effective_turn = min(turn, 10)  # cap to fit 8192 context window
    history = '\n'.join([f"Turn{t}: {USER_CONTEXTS[user_id]}" for t in range(effective_turn + 1)])
    return f"[INST] <<SYS>>\n{SYSTEM_PROMPT}\n<</SYS>>\n{history}\nProvide a brief answer: [/INST]"


async def send_request(session, endpoint, user_id, turn, sem, results, start_time):
    """Send one request, measure TTFT, record with wall-clock timestamp."""
    async with sem:
        prompt = build_prompt(user_id, turn)
        t0 = time.perf_counter()
        ttft = None
        try:
            async with session.post(
                endpoint,
                json={'model': MODEL, 'prompt': prompt, 'max_tokens': MAX_TOKENS,
                      'stream': True, 'temperature': 0.1},
                timeout=aiohttp.ClientTimeout(total=90)
            ) as resp:
                async for line in resp.content:
                    decoded = line.decode()
                    if decoded.strip().startswith('data:') and 'DONE' not in decoded:
                        if not ttft:
                            ttft = (time.perf_counter() - t0) * 1000
                        break
        except Exception:
            results.append({'ts': time.perf_counter() - start_time, 'user': user_id,
                           'turn': turn, 'ttft': -1})
            return

        results.append({'ts': time.perf_counter() - start_time, 'user': user_id,
                       'turn': turn, 'ttft': ttft or 0})


async def run_sustained(name: str, endpoint: str) -> list:
    """Run sustained benchmark with Poisson arrivals for DURATION_S seconds."""
    print(f'\n{"="*60}', flush=True)
    print(f'  {name} — {DURATION_S}s sustained, Poisson λ={TARGET_QPS} QPS', flush=True)
    print(f'{"="*60}', flush=True)

    results = []
    sem = asyncio.Semaphore(MAX_CONCURRENCY)
    user_turns = [0] * USERS  # track current turn per user
    start_time = time.perf_counter()
    tasks = []

    async with aiohttp.ClientSession() as session:
        # Generate Poisson inter-arrival times for the full duration
        elapsed = 0.0
        while elapsed < DURATION_S:
            # Poisson inter-arrival = exponential distribution
            wait = random.expovariate(TARGET_QPS)
            elapsed += wait
            if elapsed >= DURATION_S:
                break

            # Pick a random user, advance their turn
            user_id = random.randint(0, USERS - 1)
            turn = user_turns[user_id]
            user_turns[user_id] += 1

            # Schedule the request at the right wall-clock time
            delay = elapsed - (time.perf_counter() - start_time)
            if delay > 0:
                await asyncio.sleep(delay)

            task = asyncio.create_task(
                send_request(session, endpoint, user_id, turn, sem, results, start_time)
            )
            tasks.append(task)

            # Print progress every BUCKET_S
            now = time.perf_counter() - start_time
            bucket = int(now // BUCKET_S)
            bucket_results = [r for r in results if r['ttft'] > 0
                            and int(r['ts'] // BUCKET_S) == bucket]
            if len(tasks) % 50 == 0:
                pending = len([t for t in tasks if not t.done()])
                print(f'  [{now:.0f}s] sent={len(tasks)} done={len(results)} '
                      f'pending={pending}', flush=True)

        # Wait for all in-flight requests
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # Print time-series summary
    print(f'\n  Time-series (buckets of {BUCKET_S}s):', flush=True)
    print(f'  {"Bucket":>8} | {"p50":>7} | {"p90":>7} | {"n":>4} | {"err":>3}', flush=True)
    print(f'  {"-"*8}-+-{"-"*7}-+-{"-"*7}-+-{"-"*4}-+-{"-"*3}', flush=True)

    num_buckets = DURATION_S // BUCKET_S
    for b in range(num_buckets):
        bucket_data = sorted([r['ttft'] for r in results
                            if r['ttft'] > 0 and int(r['ts'] // BUCKET_S) == b])
        errors = len([r for r in results
                     if r['ttft'] <= 0 and int(r['ts'] // BUCKET_S) == b])
        if bucket_data:
            p50 = bucket_data[len(bucket_data) // 2]
            p90 = bucket_data[int(len(bucket_data) * 0.9)]
            print(f'  {b*BUCKET_S:>3}-{(b+1)*BUCKET_S:>3}s | {p50:>6.0f}ms | {p90:>6.0f}ms | '
                  f'{len(bucket_data):>4} | {errors:>3}', flush=True)
        else:
            print(f'  {b*BUCKET_S:>3}-{(b+1)*BUCKET_S:>3}s | {"N/A":>7} | {"N/A":>7} | '
                  f'{0:>4} | {errors:>3}', flush=True)

    return results


async def warmup(endpoints):
    """Send a few requests to warm up connections."""
    async with aiohttp.ClientSession() as s:
        for ep in endpoints:
            try:
                async with s.post(ep, json={'model': MODEL, 'prompt': '[INST]Hi[/INST]',
                                           'max_tokens': 3},
                                 timeout=aiohttp.ClientTimeout(total=30)) as r:
                    await r.read()
            except Exception:
                pass
    print('Warmup complete.\n', flush=True)


async def main():
    await warmup([RR, CA])

    rr_results = await run_sustained('ROUND-ROBIN', RR)
    print('\n  Cooling down 30s before CA run...', flush=True)
    await asyncio.sleep(30)
    ca_results = await run_sustained('CACHE-AWARE', CA)

    # Save raw results
    output = {
        'config': {
            'users': USERS, 'duration_s': DURATION_S, 'target_qps': TARGET_QPS,
            'max_tokens': MAX_TOKENS, 'max_concurrency': MAX_CONCURRENCY,
            'bucket_s': BUCKET_S, 'prefix_tokens': '~6000',
            'arrival_pattern': 'poisson'
        },
        'rr': rr_results,
        'ca': ca_results
    }
    with open('/tmp/sustained_results.json', 'w') as f:
        json.dump(output, f)
    print(f'\nResults saved to /tmp/sustained_results.json', flush=True)

    # Final comparison table
    print(f'\n{"="*60}', flush=True)
    print(f'  COMPARISON: RR vs CA (p50 TTFT per {BUCKET_S}s bucket)', flush=True)
    print(f'{"="*60}', flush=True)
    print(f'  {"Bucket":>8} | {"RR p50":>8} | {"CA p50":>8} | {"Improvement":>11}', flush=True)
    print(f'  {"-"*8}-+-{"-"*8}-+-{"-"*8}-+-{"-"*11}', flush=True)

    num_buckets = DURATION_S // BUCKET_S
    for b in range(num_buckets):
        rr_b = sorted([r['ttft'] for r in rr_results
                      if r['ttft'] > 0 and int(r['ts'] // BUCKET_S) == b])
        ca_b = sorted([r['ttft'] for r in ca_results
                      if r['ttft'] > 0 and int(r['ts'] // BUCKET_S) == b])
        if rr_b and ca_b:
            rr_p50 = rr_b[len(rr_b) // 2]
            ca_p50 = ca_b[len(ca_b) // 2]
            imp = (rr_p50 - ca_p50) / rr_p50 * 100
            print(f'  {b*BUCKET_S:>3}-{(b+1)*BUCKET_S:>3}s | {rr_p50:>7.0f}ms | '
                  f'{ca_p50:>7.0f}ms | {imp:>+9.1f}%', flush=True)

    print(f'\nDone. Copy results: kubectl cp inference/benchmark-runner:/tmp/sustained_results.json ./results/sustained_results.json', flush=True)


if __name__ == '__main__':
    asyncio.run(main())
