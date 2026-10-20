# Incident 2: fake-api, probe path failure

Opened 2026-10-20 14:10:57 UTC. Mode: live. Verdict: probe_fault. Action: collect_diagnostics (tier low).

## What broke

The primary probe path failed, but fake-api answered an independent client correctly. Nothing is wrong with fake-api.

## Timeline

| Time (UTC) | Step | What happened |
|---|---|---|
| 14:10:55 | sensor | failure on fake-api, located at probe (primary probe path) |
| 14:10:57 | detect | sensor raised a failure on fake-api, located at probe (primary probe path) |
| 14:10:57 | verify | not an outage: the sensor's primary probe path failed while an independent path reached fake-api |
| 14:10:59 | diagnose | The primary probe path failed, but fake-api answered an independent client correctly. Nothing is wrong with fake-api. |
| 14:10:59 | decide | collect_diagnostics at tier low: the fault is in Warden's own probe path, so fake-api is left alone |
| 14:10:59 | act | collect diagnostics done; target answering per probe #28 |

## Evidence

| Probe | Time | Target | Path | Result |
|---|---|---|---|---|
| #17 | 14:10:40 | fake-api | primary | 200 in 1 ms |
| #19 | 14:10:45 | fake-api | primary | 200 in 1 ms |
| #21 | 14:10:50 | fake-api | primary | 200 in 1 ms |
| #23 (cited) | 14:10:55 | fake-api | primary | status 500, expected 200 |
| #24 (cited) | 14:10:55 | fake-api | secondary | 200 in 1 ms |
| #25 | 14:10:55 | fake-api | canary | ok in 0 ms |
| #27 | 14:10:57 | fake-api | brain | 200 in 12 ms |
| #28 | 14:10:59 | fake-api | brain | 200 in 25 ms |

## Why this is believed to be the cause

- The sensor's primary path (ureq) saw: status 500, expected 200. Its secondary path (separate DNS, socket and TLS) saw: 200.
- The canary outside every target was reachable, so the probing machine's network was working.
- The brain probed again with a third stack (Python urllib): 200 in 12 ms.
- fake-api has no dependencies in warden.toml.
- Conclusion: not an outage: the sensor's primary probe path failed while an independent path reached fake-api.
- No model was consulted: model unavailable: Ollama at http://127.0.0.1:11434: nothing is listening there. The rule-based diagnosis stands.

## What was done

The fault is in Warden's own probe path, so fake-api is left alone.

Result: diagnostics written to diagnostics/incident-2.json.

## What was confirmed

- Probe #28: pass, 200 in 25 ms

Outcome: collect diagnostics done; target answering per probe #28.

## Cost

| Model | Calls | Tokens in | Tokens out | Probes | Wall time | CPU time |
|---|---|---|---|---|---|---|
| ollama:llama3.2 | 0 | 0 | 0 | 2 | 2.1 s | 16 ms |
