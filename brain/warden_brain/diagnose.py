"""Root cause from evidence, checked against the dependency graph.

The rule-based hypothesis is grounded by construction: it only ever names the
location the verify step established. A model's hypothesis is accepted only if
it names a component in the target's dependency closure, cites probe ids that
exist in the evidence and actually failed, and does not report a fault on a
system the fresh probes found healthy."""

from __future__ import annotations

import json
from typing import Optional


def rule_hypothesis(verdict: dict, evidence: list, target, event_detail: Optional[dict] = None) -> dict:
    location = verdict["location"]
    culprit = verdict.get("culprit") or target.name
    failing = [p for p in evidence if not p["ok"] and p["target"] == (culprit if location != "probe" else target.name)]
    cited = [p["id"] for p in failing][-5:]

    if verdict["verdict"] == "degrading":
        return {
            "component": target.name,
            "cause": "latency drift",
            "claim": f"{target.name} is slowing down: {verdict.get('reason', 'latency above its usual range')}.",
            "evidence": cited or [p["id"] for p in evidence if p["target"] == target.name][-5:],
        }
    if location == "probe":
        ids = (event_detail or {}).get("probe_ids") or {}
        known = {p["id"] for p in evidence}
        cited = [ids[k] for k in ("primary", "secondary") if ids.get(k) in known] or cited
        return {
            "component": "probe",
            "cause": "probe path failure",
            "claim": (
                f"The {culprit} failed, but {target.name} answered an independent client correctly. "
                f"Nothing is wrong with {target.name}."
            ),
            "evidence": cited,
        }
    if location == "dependency":
        return {
            "component": culprit,
            "cause": "upstream dependency down",
            "claim": f"{culprit} is failing and {target.name} depends on it, so {target.name} fails with it.",
            "evidence": cited,
        }

    last = failing[-1] if failing else {}
    error = (last.get("error") or "").lower()
    status = last.get("status")
    if status is not None and status >= 500:
        cause, claim = "application error", f"{target.name} is reachable but its application returns {status}."
    elif status is not None:
        cause, claim = "wrong status", f"{target.name} returns {status} where {target.expected_status} is expected."
    elif "heartbeat" in error or "does not exist" in error:
        cause, claim = "process stopped", f"The process behind {target.name} has stopped writing its heartbeat ({last.get('error')})."
    elif "dns" in error or "getaddrinfo" in error:
        cause, claim = "name resolution", f"{target.name}'s hostname does not resolve."
    elif "tls" in error or "certificate" in error:
        cause, claim = "tls failure", f"TLS to {target.name} fails: {last.get('error')}."
    elif "timed out" in error or "timeout" in error:
        cause, claim = "not answering", f"{target.name} accepts no response within the timeout."
    else:
        cause, claim = "unreachable", f"{target.name} is unreachable: {last.get('error') or 'no answer'}."
    return {"component": target.name, "cause": cause, "claim": claim, "evidence": cited}


def model_prompt(target, closure: list, verdict: dict, evidence: list) -> str:
    rows = [
        {k: p[k] for k in ("id", "ts", "target", "path", "ok", "status", "latency_ms", "error")}
        for p in evidence
    ]
    return (
        "You are diagnosing one incident for a reliability system. Answer with one JSON object only:\n"
        '{"verdict": "healthy" or "fault", "component": "<one name from COMPONENTS or probe>", '
        '"claim": "<one sentence>", "evidence": [<probe ids from EVIDENCE that support the claim>]}\n'
        "Say healthy if the evidence does not show a fault. Cite only ids that appear below.\n\n"
        f"TARGET: {target.name} ({target.kind} {target.endpoint})\n"
        f"COMPONENTS: {json.dumps([target.name] + closure)}\n"
        f"VERIFIED: {json.dumps({k: verdict.get(k) for k in ('verdict', 'location', 'culprit')})}\n"
        f"EVIDENCE: {json.dumps(rows)}\n"
    )


def ground(claim: Optional[dict], target_name: str, closure: list, evidence: list, fresh_healthy: bool) -> tuple:
    """Returns (accepted, reason)."""
    if not isinstance(claim, dict):
        return False, "the model did not return a JSON object"
    verdict = claim.get("verdict")
    component = claim.get("component")
    cited = claim.get("evidence") or []
    if verdict not in ("healthy", "fault"):
        return False, f"verdict {verdict!r} is not healthy or fault"
    if verdict == "fault" and fresh_healthy:
        return False, "the model reported a fault but every fresh probe of the target and its dependencies passed"
    if verdict == "healthy":
        if fresh_healthy:
            return True, "agrees with the fresh probes"
        return False, "the model reported healthy but both sensor paths and the brain's own probe failed"
    allowed = {target_name, "probe", *closure}
    if component not in allowed:
        return False, f"{component!r} is not {target_name} or one of its dependencies {sorted(closure)}"
    by_id = {p["id"]: p for p in evidence}
    if not isinstance(cited, list) or not cited:
        return False, "the claim cites no evidence"
    unknown = [c for c in cited if c not in by_id]
    if unknown:
        return False, f"cites probe ids that do not exist: {unknown}"
    if component != "probe" and not any(not by_id[c]["ok"] and by_id[c]["target"] == component for c in cited):
        return False, f"none of the cited probes show {component} failing"
    return True, "names a component in the dependency graph and cites failing probes of it"
