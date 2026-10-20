"""Plain-English incident report, saved as markdown next to the others."""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import store


def _cell(value) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def _clock(ts: str) -> str:
    return ts[11:19] if ts and len(ts) >= 19 else ts or ""


def render(ctx: dict, transitions: list, evidence: list) -> str:
    out, target, event = ctx["out"], ctx["target"], ctx["event"]
    verify, diag, decide, act = out.get("verify", {}), out.get("diagnose", {}), out.get("decide", {}), out.get("act", {})
    hyp = diag.get("hypothesis", {})
    cost = ctx["cost"]
    opened = transitions[0]["ts"] if transitions else event.get("ts", "")
    title = f"Incident {ctx['incident_id']}: {target.name}, {hyp.get('cause', verify.get('verdict', 'event'))}"

    lines = [
        f"# {title}",
        "",
        f"Opened {opened[:10]} {opened[11:19]} UTC. Mode: {ctx['mode']}. Verdict: {verify.get('verdict')}. "
        f"Action: {decide.get('action', 'none')} (tier {decide.get('tier') or 'none'}).",
        "",
        "## What broke",
        "",
        hyp.get("claim") or verify.get("summary", ""),
        "",
        "## Timeline",
        "",
        "| Time (UTC) | Step | What happened |",
        "|---|---|---|",
    ]
    lines.append(f"| {_clock(event.get('ts', ''))} | sensor | {_cell(_event_line(event))} |")
    for t in transitions:
        output = json.loads(t["output"]) if isinstance(t["output"], str) else t["output"]
        lines.append(f"| {_clock(t['ts'])} | {t['state']} | {_cell(output.get('summary', ''))} |")

    lines += ["", "## Evidence", "", "| Probe | Time | Target | Path | Result |", "|---|---|---|---|---|"]
    cited = set(hyp.get("evidence") or [])
    for p in evidence[-14:]:
        mark = " (cited)" if p["id"] in cited else ""
        result = p["error"] or (f"{p['status']} in {p['latency_ms']:.0f} ms" if p["status"] else f"ok in {p['latency_ms']:.0f} ms")
        lines.append(f"| #{p['id']}{mark} | {_clock(p['ts'])} | {p['target']} | {p['path']} | {_cell(result)} |")

    lines += ["", "## Why this is believed to be the cause", ""]
    lines += _reasoning(ctx)

    lines += ["", "## What was done", ""]
    why = decide.get("why", "")
    lines.append(f"{why[:1].upper()}{why[1:]}." if why else "Nothing.")
    if act.get("result"):
        lines += ["", f"Result: {act['result']}."]
    if act.get("rollback_plan"):
        lines += ["", f"Recommendation: {act.get('recommendation')}", ""]
        lines += [f"{i}. {step}" for i, step in enumerate(act["rollback_plan"], 1)]

    lines += ["", "## What was confirmed", ""]
    conf = act.get("confirmation") or {}
    for c in conf.get("probes", []):
        lines.append(f"- Probe #{c['probe_id']}: {'pass' if c['ok'] else 'fail'}, {c['result']}")
    lines += ["", f"Outcome: {act.get('outcome', 'none')}."]

    lines += [
        "",
        "## Cost",
        "",
        "| Model | Calls | Tokens in | Tokens out | Probes | Wall time | CPU time |",
        "|---|---|---|---|---|---|---|",
        f"| {cost['model']} | {cost['model_calls']} | {cost['tokens_in']} | {cost['tokens_out']} | "
        f"{cost['probes']} | {_duration(cost['wall_ms'])} | {cost['cpu_ms']:.0f} ms |",
        "",
    ]
    return "\n".join(lines)


def _duration(ms: float) -> str:
    return f"{ms:.0f} ms" if ms < 1000 else f"{ms / 1000:.1f} s"


def _event_line(event: dict) -> str:
    if event["kind"] == "drift":
        reason = ((event.get("detail") or {}).get("drift") or {}).get("reason", "")
        return f"latency drift on {event['target']}: {reason}"
    return f"failure on {event['target']}, located at {event['fault']} ({event.get('culprit')})"


def _reasoning(ctx: dict) -> list:
    out, target, event = ctx["out"], ctx["target"], ctx["event"]
    verify, diag = out.get("verify", {}), out.get("diagnose", {})
    detail = event.get("detail") or {}
    lines = []
    if event["kind"] == "failure":
        p, s, c = detail.get("primary") or {}, detail.get("secondary") or {}, detail.get("canary") or {}
        lines.append(
            f"- The sensor's primary path (ureq) saw: {p.get('error') or p.get('status')}. "
            f"Its secondary path (separate DNS, socket and TLS) saw: {s.get('error') or s.get('status')}."
        )
        lines.append(f"- The canary outside every target was {'reachable' if c.get('ok') else 'unreachable'}, "
                     f"so the probing machine's network {'was working' if c.get('ok') else 'was the problem'}.")
    bp = verify.get("brain_probe") or {}
    lines.append(f"- The brain probed again with a third stack (Python urllib): {bp.get('result')}.")
    deps = verify.get("dependencies") or []
    if deps:
        lines.append("- Dependencies, from the graph in warden.toml: "
                     + "; ".join(f"{d['name']} {'passed' if d['ok'] else 'failed'}" for d in deps) + ".")
    else:
        lines.append(f"- {target.name} has no dependencies in warden.toml.")
    lines.append(f"- Conclusion: {verify.get('summary')}.")
    model = diag.get("model")
    if model:
        claim = (model.get("claim") or {}).get("claim") if isinstance(model.get("claim"), dict) else None
        if model.get("accepted") and model.get("used"):
            lines.append(f"- The model ({ctx['cost']['model']}) agreed and worded the claim above. Grounding check: {model['reason']}.")
        elif model.get("claim") is None:
            lines.append(f"- No model was consulted: {model['reason']}. The rule-based diagnosis stands.")
        else:
            lines.append(f"- The model claimed: \"{claim}\". Rejected: {model['reason']}. The rule-based diagnosis stands.")
    return lines


def write(conn, ctx: dict, reports_dir: Path):
    transitions = [dict(r) for r in conn.execute(
        "SELECT ts, state, output FROM transitions WHERE incident_id = ? ORDER BY id", (ctx["incident_id"],)
    )]
    evidence = store.recent_probes(conn, [ctx["target"].name] + ctx["closure"], minutes=15, limit=40)
    md = render(ctx, transitions, evidence)
    reports_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9-]+", "-", ctx["target"].name.lower()).strip("-")
    path = reports_dir / f"incident-{ctx['incident_id']:04d}-{slug}.md"
    path.write_text(md, encoding="utf-8")
    return md, path
