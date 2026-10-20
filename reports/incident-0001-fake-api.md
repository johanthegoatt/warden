# Incident 1: fake-api, application error

Opened 2026-10-20 14:00:39 UTC. Mode: live. Verdict: fault. Action: restart (tier low).

## What broke

fake-api is reachable but its application returns 500.

## Timeline

| Time (UTC) | Step | What happened |
|---|---|---|
| 14:00:38 | sensor | failure on fake-api, located at target (fake-api) |
| 14:00:39 | detect | sensor raised a failure on fake-api, located at target (fake-api) |
| 14:00:39 | verify | confirmed by a third probe stack: status 500, expected 200; dependencies pass |
| 14:00:41 | diagnose | fake-api is reachable but its application returns 500. |
| 14:00:41 | decide | restart at tier low: fake-api is marked stateless, so a restart loses nothing |
| 14:00:41 | act | fixed: restart ran and probe #16 passed |

## Evidence

| Probe | Time | Target | Path | Result |
|---|---|---|---|---|
| #1 | 14:00:13 | fake-api | primary | 200 in 1 ms |
| #3 | 14:00:18 | fake-api | primary | 200 in 1 ms |
| #5 | 14:00:23 | fake-api | primary | 200 in 1 ms |
| #7 | 14:00:28 | fake-api | primary | 200 in 1 ms |
| #9 | 14:00:33 | fake-api | primary | 200 in 1 ms |
| #11 (cited) | 14:00:38 | fake-api | primary | status 500, expected 200 |
| #12 (cited) | 14:00:38 | fake-api | secondary | status 500, expected 200 |
| #13 | 14:00:38 | fake-api | canary | ok in 0 ms |
| #15 (cited) | 14:00:39 | fake-api | brain | status 500, expected 200 |
| #16 | 14:00:41 | fake-api | brain | 200 in 16 ms |

## Why this is believed to be the cause

- The sensor's primary path (ureq) saw: status 500, expected 200. Its secondary path (separate DNS, socket and TLS) saw: status 500, expected 200.
- The canary outside every target was reachable, so the probing machine's network was working.
- The brain probed again with a third stack (Python urllib): status 500, expected 200.
- fake-api has no dependencies in warden.toml.
- Conclusion: confirmed by a third probe stack: status 500, expected 200; dependencies pass.
- No model was consulted: model unavailable: Ollama at http://127.0.0.1:11434: nothing is listening there. The rule-based diagnosis stands.

## What was done

Fake-api is marked stateless, so a restart loses nothing.

Result: POST http://127.0.0.1:8098/restart answered 200.

## What was confirmed

- Probe #16: pass, 200 in 16 ms

Outcome: fixed: restart ran and probe #16 passed.

## Cost

| Model | Calls | Tokens in | Tokens out | Probes | Wall time | CPU time |
|---|---|---|---|---|---|---|
| ollama:llama3.2 | 0 | 0 | 0 | 2 | 2.1 s | 0 ms |
