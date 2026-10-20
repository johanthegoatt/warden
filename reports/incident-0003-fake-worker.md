# Incident 3: fake-worker, unreachable

Opened 2026-10-20 14:11:17 UTC. Mode: live. Verdict: fault. Action: restart (tier medium).

## What broke

fake-worker is unreachable: RemoteDisconnected: Remote end closed connection without response.

## Timeline

| Time (UTC) | Step | What happened |
|---|---|---|
| 14:11:15 | sensor | failure on fake-worker, located at target (fake-worker) |
| 14:11:17 | detect | sensor raised a failure on fake-worker, located at target (fake-worker) |
| 14:11:17 | verify | confirmed by a third probe stack: RemoteDisconnected: Remote end closed connection without response; dependencies pass |
| 14:11:19 | diagnose | fake-worker is unreachable: RemoteDisconnected: Remote end closed connection without response. |
| 14:11:19 | decide | restart at tier medium: fake-worker may hold state, so a restart needs approval |
| 14:11:19 | act | waiting for approval to restart fake-worker |
| 14:11:25 | act | fixed: restart ran and probe #46 passed |

## Evidence

| Probe | Time | Target | Path | Result |
|---|---|---|---|---|
| #33 | 14:11:10 | fake-api | primary | 200 in 1 ms |
| #34 | 14:11:10 | fake-worker | primary | 200 in 1 ms |
| #35 | 14:11:15 | fake-api | primary | 200 in 1 ms |
| #36 (cited) | 14:11:15 | fake-worker | primary | io: Peer disconnected |
| #37 (cited) | 14:11:15 | fake-worker | secondary | connection closed before a status line |
| #38 | 14:11:15 | fake-worker | canary | ok in 0 ms |
| #39 (cited) | 14:11:17 | fake-worker | brain | RemoteDisconnected: Remote end closed connection without response |
| #40 | 14:11:17 | fake-api | brain | 200 in 14 ms |
| #41 | 14:11:20 | fake-api | primary | 200 in 1 ms |
| #42 | 14:11:20 | fake-worker | primary | io: Peer disconnected |
| #43 | 14:11:20 | fake-worker | secondary | connection closed before a status line |
| #44 | 14:11:20 | fake-worker | canary | ok in 0 ms |
| #45 | 14:11:25 | fake-api | primary | 200 in 1 ms |
| #46 | 14:11:25 | fake-worker | brain | 200 in 15 ms |

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

- Probe #46: pass, 200 in 15 ms

Outcome: fixed: restart ran and probe #46 passed.

## Cost

| Model | Calls | Tokens in | Tokens out | Probes | Wall time | CPU time |
|---|---|---|---|---|---|---|
| ollama:llama3.2 | 0 | 0 | 0 | 3 | 2.1 s | 0 ms |
