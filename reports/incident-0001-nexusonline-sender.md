# Incident 1: nexusonline-sender, process stopped

Opened 2026-10-20 14:19:54 UTC. Mode: dry-run. Verdict: fault. Action: rollback (tier high).

## What broke

The process behind nexusonline-sender has stopped writing its heartbeat (last heartbeat 1676272s ago).

## Timeline

| Time (UTC) | Step | What happened |
|---|---|---|
| 14:19:45 | sensor | failure on nexusonline-sender, located at target (nexusonline-sender) |
| 14:19:54 | detect | sensor raised a failure on nexusonline-sender, located at target (nexusonline-sender) |
| 14:19:55 | verify | confirmed by a third probe stack: last heartbeat 1676272s ago; dependencies pass |
| 14:19:55 | diagnose | The process behind nexusonline-sender has stopped writing its heartbeat (last heartbeat 1676272s ago). |
| 14:19:55 | decide | rollback at tier high: nexusonline-sender has no safe restart configured; changing a deployment is a decision for a person |
| 14:19:55 | act | recommendation written, nothing executed; target still failing per probe #10 |

## Evidence

| Probe | Time | Target | Path | Result |
|---|---|---|---|---|
| #1 | 14:19:44 | discord-api | primary | 200 in 237 ms |
| #3 (cited) | 14:19:45 | nexusonline-sender | primary | last heartbeat 1676262s ago, limit is 3600s |
| #4 (cited) | 14:19:45 | nexusonline-sender | secondary | last heartbeat 1676262s ago, limit is 3600s |
| #5 | 14:19:45 | nexusonline-sender | canary | ok in 36 ms |
| #8 (cited) | 14:19:54 | nexusonline-sender | brain | last heartbeat 1676272s ago |
| #9 | 14:19:55 | discord-api | brain | 200 in 244 ms |
| #10 | 14:19:55 | nexusonline-sender | brain | last heartbeat 1676272s ago |

## Why this is believed to be the cause

- The sensor's primary path (ureq) saw: last heartbeat 1676262s ago, limit is 3600s. Its secondary path (separate DNS, socket and TLS) saw: last heartbeat 1676262s ago, limit is 3600s.
- The canary outside every target was reachable, so the probing machine's network was working.
- The brain probed again with a third stack (Python urllib): last heartbeat 1676272s ago.
- Dependencies, from the graph in warden.toml: discord-api passed.
- Conclusion: confirmed by a third probe stack: last heartbeat 1676272s ago; dependencies pass.

## What was done

Nexusonline-sender has no safe restart configured; changing a deployment is a decision for a person.

Result: recommendation only; high-tier actions are never executed.

Recommendation: Roll nexusonline-sender back to its previous deployment.

1. Run: redeploy the last version of nexusonline-sender that passed its probes
2. Watch the dashboard until nexusonline-sender passes three probes in a row.
3. If the previous deployment also fails, the cause is outside the code: check its dependencies and provider status.

## What was confirmed

- Probe #10: fail, last heartbeat 1676272s ago

Outcome: recommendation written, nothing executed; target still failing per probe #10.

## Cost

| Model | Calls | Tokens in | Tokens out | Probes | Wall time | CPU time |
|---|---|---|---|---|---|---|
| none | 0 | 0 | 0 | 3 | 247 ms | 47 ms |
