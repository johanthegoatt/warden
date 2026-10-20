# Incident 3: fake-worker, unreachable

Opened 2026-10-20 14:01:19 UTC. Mode: live. Verdict: fault. Action: restart (tier medium).

## What broke

fake-worker is unreachable: RemoteDisconnected: Remote end closed connection without response.

## Timeline

| Time (UTC) | Step | What happened |
|---|---|---|
| 14:01:18 | sensor | failure on fake-worker, located at target (fake-worker) |
| 14:01:19 | detect | sensor raised a failure on fake-worker, located at target (fake-worker) |
| 14:01:20 | verify | confirmed by a third probe stack: RemoteDisconnected: Remote end closed connection without response; dependencies pass |
| 14:01:22 | diagnose | fake-worker is unreachable: RemoteDisconnected: Remote end closed connection without response. |
| 14:01:22 | decide | restart at tier medium: fake-worker may hold state, so a restart needs approval |
| 14:01:22 | act | waiting for approval to restart fake-worker |
| 14:01:28 | act | fixed: restart ran and probe #45 passed |

## Evidence

| Probe | Time | Target | Path | Result |
|---|---|---|---|---|
| #32 | 14:01:08 | fake-worker | primary | 200 in 1 ms |
| #33 | 14:01:13 | fake-api | primary | 200 in 1 ms |
| #34 | 14:01:13 | fake-worker | primary | 200 in 1 ms |
| #35 | 14:01:18 | fake-api | primary | 200 in 1 ms |
| #36 (cited) | 14:01:18 | fake-worker | primary | io: Peer disconnected |
| #37 (cited) | 14:01:18 | fake-worker | secondary | connection closed before a status line |
| #38 | 14:01:18 | fake-worker | canary | ok in 0 ms |
| #39 (cited) | 14:01:20 | fake-worker | brain | RemoteDisconnected: Remote end closed connection without response |
| #40 | 14:01:20 | fake-api | brain | 200 in 14 ms |
| #41 | 14:01:23 | fake-api | primary | 200 in 1 ms |
| #42 | 14:01:23 | fake-worker | primary | io: Peer disconnected |
| #43 | 14:01:23 | fake-worker | secondary | connection closed before a status line |
| #44 | 14:01:23 | fake-worker | canary | ok in 0 ms |
| #45 | 14:01:28 | fake-worker | brain | 200 in 16 ms |

## Why this is believed to be the cause

- The sensor's primary path (ureq) saw: io: Peer disconnected. Its secondary path (separate DNS, socket and TLS) saw: connection closed before a status line.
- The canary outside every target was reachable, so the probing machine's network was working.
- The brain probed again with a third stack (Python urllib): RemoteDisconnected: Remote end closed connection without response.
- Dependencies, from the graph in warden.toml: fake-api passed.
- Conclusion: confirmed by a third probe stack: RemoteDisconnected: Remote end closed connection without response; dependencies pass.
- No model was consulted: model unavailable: Ollama at http://127.0.0.1:11434: nothing is listening there. The rule-based diagnosis stands.

## What was done

Fake-worker may hold state, so a restart needs approval.

Result: POST http://127.0.0.1:8096/restart answered 200.

## What was confirmed

- Probe #45: pass, 200 in 16 ms

Outcome: fixed: restart ran and probe #45 passed.

## Cost

| Model | Calls | Tokens in | Tokens out | Probes | Wall time | CPU time |
|---|---|---|---|---|---|---|
| ollama:llama3.2 | 0 | 0 | 0 | 3 | 2.1 s | 0 ms |
