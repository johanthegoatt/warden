//! A small HTTP server on std sockets: the dashboard files, a JSON read API
//! over the SQLite file, and one write route for approving medium-tier actions.

use crate::store;
use rusqlite::{Connection, OptionalExtension, params};
use serde_json::{Value, json};
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

const INDEX: &str = include_str!("../../dashboard/index.html");
const CSS: &str = include_str!("../../dashboard/app.css");
const JS: &str = include_str!("../../dashboard/app.js");

pub struct Settings {
    pub db: PathBuf,
    /// When set, POST requests must carry it in X-Warden-Token.
    pub token: Option<String>,
}

pub fn serve(addr: &str, settings: Settings) -> std::io::Result<()> {
    let listener = TcpListener::bind(addr)?;
    let settings = Arc::new(settings);
    for stream in listener.incoming().flatten() {
        let s = settings.clone();
        std::thread::spawn(move || {
            let _ = handle(stream, &s);
        });
    }
    Ok(())
}

struct Request {
    method: String,
    path: String,
    headers: Vec<(String, String)>,
    body: String,
}

impl Request {
    fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .find(|(k, _)| k.eq_ignore_ascii_case(name))
            .map(|(_, v)| v.as_str())
    }
}

fn read_request(stream: &mut TcpStream) -> Option<Request> {
    stream.set_read_timeout(Some(Duration::from_secs(5))).ok()?;
    let mut buf = Vec::new();
    let mut chunk = [0u8; 2048];
    let head_end = loop {
        if let Some(i) = buf.windows(4).position(|w| w == b"\r\n\r\n") {
            break i;
        }
        if buf.len() > 32 * 1024 {
            return None;
        }
        let n = stream.read(&mut chunk).ok()?;
        if n == 0 {
            return None;
        }
        buf.extend_from_slice(&chunk[..n]);
    };
    let head = String::from_utf8_lossy(&buf[..head_end]).into_owned();
    let mut lines = head.split("\r\n");
    let mut first = lines.next()?.split_whitespace();
    let method = first.next()?.to_string();
    let path = first.next()?.to_string();
    let headers: Vec<(String, String)> = lines
        .filter_map(|l| l.split_once(':'))
        .map(|(k, v)| (k.trim().to_string(), v.trim().to_string()))
        .collect();
    let len: usize = headers
        .iter()
        .find(|(k, _)| k.eq_ignore_ascii_case("content-length"))
        .and_then(|(_, v)| v.parse().ok())
        .unwrap_or(0);
    if len > 4096 {
        return None;
    }
    let mut body = buf[head_end + 4..].to_vec();
    while body.len() < len {
        let n = stream.read(&mut chunk).ok()?;
        if n == 0 {
            break;
        }
        body.extend_from_slice(&chunk[..n]);
    }
    body.truncate(len);
    Some(Request {
        method,
        path,
        headers,
        body: String::from_utf8_lossy(&body).into_owned(),
    })
}

fn respond(stream: &mut TcpStream, status: u16, content_type: &str, cache: &str, body: &[u8]) -> std::io::Result<()> {
    let reason = match status {
        200 => "OK",
        400 => "Bad Request",
        403 => "Forbidden",
        404 => "Not Found",
        405 => "Method Not Allowed",
        409 => "Conflict",
        _ => "Internal Server Error",
    };
    write!(
        stream,
        "HTTP/1.1 {status} {reason}\r\nContent-Type: {content_type}\r\nContent-Length: {}\r\nCache-Control: {cache}\r\n\
         Content-Security-Policy: default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'\r\n\
         X-Content-Type-Options: nosniff\r\nReferrer-Policy: no-referrer\r\nConnection: close\r\n\r\n",
        body.len()
    )?;
    stream.write_all(body)
}

fn problem(stream: &mut TcpStream, status: u16, detail: &str) -> std::io::Result<()> {
    let body = json!({ "status": status, "detail": detail }).to_string();
    respond(stream, status, "application/problem+json", "no-store", body.as_bytes())
}

fn handle(mut stream: TcpStream, settings: &Settings) -> std::io::Result<()> {
    let Some(req) = read_request(&mut stream) else {
        return problem(&mut stream, 400, "could not read the request");
    };
    let path = req.path.split('?').next().unwrap_or("").to_string();
    let segments: Vec<&str> = path.trim_matches('/').split('/').collect();
    match (req.method.as_str(), segments.as_slice()) {
        ("GET", [""]) => respond(&mut stream, 200, "text/html; charset=utf-8", "no-cache", INDEX.as_bytes()),
        ("GET", ["app.css"]) => respond(&mut stream, 200, "text/css; charset=utf-8", "no-cache", CSS.as_bytes()),
        ("GET", ["app.js"]) => respond(&mut stream, 200, "text/javascript; charset=utf-8", "no-cache", JS.as_bytes()),
        ("GET", ["api", "overview"]) => json_route(&mut stream, settings, overview),
        ("GET", ["api", "incidents", id]) => match id.parse::<i64>() {
            Ok(id) => json_route(&mut stream, settings, |c| incident(c, id)),
            Err(_) => problem(&mut stream, 400, "incident id must be a number"),
        },
        ("GET", ["api", "reports", id]) => {
            let Ok(id) = id.parse::<i64>() else {
                return problem(&mut stream, 400, "report id must be a number");
            };
            let conn = match store::open(&settings.db) {
                Ok(c) => c,
                Err(e) => return problem(&mut stream, 500, &format!("database: {e}")),
            };
            let md: Option<Option<String>> = conn
                .query_row("SELECT report_md FROM incidents WHERE id = ?1", params![id], |r| r.get(0))
                .optional()
                .unwrap_or(None);
            match md.flatten() {
                Some(md) => respond(&mut stream, 200, "text/markdown; charset=utf-8", "no-cache", md.as_bytes()),
                None => problem(&mut stream, 404, "no report for that incident yet"),
            }
        }
        ("POST", ["api", "approvals", id]) => decide(&mut stream, settings, &req, id),
        ("GET", _) => problem(&mut stream, 404, "nothing here"),
        _ => problem(&mut stream, 405, "method not allowed"),
    }
}

fn json_route<F>(stream: &mut TcpStream, settings: &Settings, f: F) -> std::io::Result<()>
where
    F: FnOnce(&Connection) -> rusqlite::Result<Value>,
{
    let result = store::open(&settings.db).and_then(|c| f(&c));
    match result {
        Ok(v) => respond(stream, 200, "application/json", "no-store", v.to_string().as_bytes()),
        Err(e) => problem(stream, 500, &format!("database: {e}")),
    }
}

fn decide(stream: &mut TcpStream, settings: &Settings, req: &Request, id: &str) -> std::io::Result<()> {
    // A custom header cannot be sent cross-site without a CORS preflight, which this server never grants.
    if req.header("x-warden-request") != Some("1") {
        return problem(stream, 403, "missing X-Warden-Request header");
    }
    if let (Some(origin), Some(host)) = (req.header("origin"), req.header("host")) {
        if origin != format!("http://{host}") {
            return problem(stream, 403, "cross-origin approvals are refused");
        }
    }
    if let Some(token) = &settings.token {
        if req.header("x-warden-token") != Some(token.as_str()) {
            return problem(stream, 403, "approval token missing or wrong");
        }
    }
    let Ok(id) = id.parse::<i64>() else {
        return problem(stream, 400, "approval id must be a number");
    };
    let decision = match req.body.trim() {
        "approve" => "approved",
        "deny" => "denied",
        _ => return problem(stream, 400, "body must be approve or deny"),
    };
    let conn = match store::open(&settings.db) {
        Ok(c) => c,
        Err(e) => return problem(stream, 500, &format!("database: {e}")),
    };
    let changed = conn
        .execute(
            &format!("UPDATE approvals SET decision = ?1, decided_at = {} WHERE id = ?2 AND decision = 'pending'", store::NOW),
            params![decision, id],
        )
        .unwrap_or(0);
    if changed == 1 {
        let body = json!({ "id": id, "decision": decision }).to_string();
        respond(stream, 200, "application/json", "no-store", body.as_bytes())
    } else {
        problem(stream, 409, "that approval is not pending any more")
    }
}

fn parse_json(s: Option<String>) -> Value {
    s.and_then(|s| serde_json::from_str(&s).ok()).unwrap_or(Value::Null)
}

fn overview(conn: &Connection) -> rusqlite::Result<Value> {
    let mut targets = Vec::new();
    let mut stmt = conn.prepare(
        "SELECT name, kind, endpoint, expected_status, latency_budget_ms, depends_on, stateless FROM targets ORDER BY name",
    )?;
    let rows: Vec<(String, String, String, Option<i64>, i64, String, i64)> = stmt
        .query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?, r.get(6)?)))?
        .collect::<Result<_, _>>()?;
    let mut probe_stmt = conn.prepare(
        "SELECT ts, ok, status, latency_ms, error FROM probes WHERE target = ?1 AND path = 'primary' ORDER BY id DESC LIMIT 48",
    )?;
    let mut event_stmt = conn.prepare(
        "SELECT kind, fault, culprit, ts, detail FROM events WHERE target = ?1 ORDER BY id DESC LIMIT 1",
    )?;
    for (name, kind, endpoint, expected, budget, deps, stateless) in rows {
        let mut probes: Vec<Value> = probe_stmt
            .query_map(params![name], |r| {
                Ok(json!({
                    "ts": r.get::<_, String>(0)?,
                    "ok": r.get::<_, i64>(1)? == 1,
                    "status": r.get::<_, Option<i64>>(2)?,
                    "latency_ms": r.get::<_, Option<f64>>(3)?,
                    "error": r.get::<_, Option<String>>(4)?,
                }))
            })?
            .collect::<Result<_, _>>()?;
        probes.reverse();
        let last_event = event_stmt
            .query_row(params![name], |r| {
                let detail: String = r.get(4)?;
                let detail: Value = serde_json::from_str(&detail).unwrap_or(Value::Null);
                Ok(json!({
                    "kind": r.get::<_, String>(0)?,
                    "fault": r.get::<_, String>(1)?,
                    "culprit": r.get::<_, Option<String>>(2)?,
                    "ts": r.get::<_, String>(3)?,
                    "drift": detail.get("drift").cloned().unwrap_or(Value::Null),
                }))
            })
            .optional()?;
        targets.push(json!({
            "name": name,
            "kind": kind,
            "endpoint": endpoint,
            "expected_status": expected,
            "latency_budget_ms": budget,
            "depends_on": serde_json::from_str::<Value>(&deps).unwrap_or(json!([])),
            "stateless": stateless == 1,
            "probes": probes,
            "last_event": last_event,
        }));
    }

    let incidents: Vec<Value> = conn
        .prepare(
            "SELECT id, target, opened_at, closed_at, state, verdict, tier, action, outcome, cost, report_md IS NOT NULL
             FROM incidents ORDER BY id DESC LIMIT 30",
        )?
        .query_map([], |r| {
            Ok(json!({
                "id": r.get::<_, i64>(0)?,
                "target": r.get::<_, String>(1)?,
                "opened_at": r.get::<_, String>(2)?,
                "closed_at": r.get::<_, Option<String>>(3)?,
                "state": r.get::<_, String>(4)?,
                "verdict": r.get::<_, Option<String>>(5)?,
                "tier": r.get::<_, Option<String>>(6)?,
                "action": r.get::<_, Option<String>>(7)?,
                "outcome": r.get::<_, Option<String>>(8)?,
                "cost": parse_json(r.get(9)?),
                "has_report": r.get::<_, bool>(10)?,
            }))
        })?
        .collect::<Result<_, _>>()?;

    let approvals: Vec<Value> = conn
        .prepare(
            "SELECT a.id, a.incident_id, a.action, a.requested_at, i.target, i.verdict
             FROM approvals a JOIN incidents i ON i.id = a.incident_id
             WHERE a.decision = 'pending' ORDER BY a.id",
        )?
        .query_map([], |r| {
            Ok(json!({
                "id": r.get::<_, i64>(0)?,
                "incident_id": r.get::<_, i64>(1)?,
                "action": r.get::<_, String>(2)?,
                "requested_at": r.get::<_, String>(3)?,
                "target": r.get::<_, String>(4)?,
                "verdict": r.get::<_, Option<String>>(5)?,
            }))
        })?
        .collect::<Result<_, _>>()?;

    let events: Vec<Value> = conn
        .prepare("SELECT id, ts, target, kind, fault, culprit, score FROM events ORDER BY id DESC LIMIT 25")?
        .query_map([], |r| {
            Ok(json!({
                "id": r.get::<_, i64>(0)?,
                "ts": r.get::<_, String>(1)?,
                "target": r.get::<_, String>(2)?,
                "kind": r.get::<_, String>(3)?,
                "fault": r.get::<_, String>(4)?,
                "culprit": r.get::<_, Option<String>>(5)?,
                "score": r.get::<_, Option<f64>>(6)?,
            }))
        })?
        .collect::<Result<_, _>>()?;

    let restarts: Vec<Value> = conn
        .prepare("SELECT ts, task, reason, restarts FROM supervisor ORDER BY id DESC LIMIT 10")?
        .query_map([], |r| {
            Ok(json!({
                "ts": r.get::<_, String>(0)?,
                "task": r.get::<_, String>(1)?,
                "reason": r.get::<_, String>(2)?,
                "restarts": r.get::<_, i64>(3)?,
            }))
        })?
        .collect::<Result<_, _>>()?;

    let heartbeats: Vec<Value> = conn
        .prepare("SELECT component, ts, note FROM heartbeat ORDER BY component")?
        .query_map([], |r| {
            Ok(json!({
                "component": r.get::<_, String>(0)?,
                "ts": r.get::<_, String>(1)?,
                "note": parse_json(r.get(2)?),
            }))
        })?
        .collect::<Result<_, _>>()?;

    let now: String = conn.query_row(&format!("SELECT {}", store::NOW), [], |r| r.get(0))?;
    Ok(json!({
        "now": now,
        "targets": targets,
        "incidents": incidents,
        "approvals": approvals,
        "events": events,
        "restarts": restarts,
        "heartbeats": heartbeats,
    }))
}

fn incident(conn: &Connection, id: i64) -> rusqlite::Result<Value> {
    let transitions: Vec<Value> = conn
        .prepare("SELECT ts, state, input, output, duration_ms FROM transitions WHERE incident_id = ?1 ORDER BY id")?
        .query_map(params![id], |r| {
            Ok(json!({
                "ts": r.get::<_, String>(0)?,
                "state": r.get::<_, String>(1)?,
                "input": parse_json(r.get(2)?),
                "output": parse_json(r.get(3)?),
                "duration_ms": r.get::<_, f64>(4)?,
            }))
        })?
        .collect::<Result<_, _>>()?;
    let head = conn
        .query_row(
            "SELECT target, state, verdict, tier, action, outcome, opened_at, closed_at FROM incidents WHERE id = ?1",
            params![id],
            |r| {
                Ok(json!({
                    "id": id,
                    "target": r.get::<_, String>(0)?,
                    "state": r.get::<_, String>(1)?,
                    "verdict": r.get::<_, Option<String>>(2)?,
                    "tier": r.get::<_, Option<String>>(3)?,
                    "action": r.get::<_, Option<String>>(4)?,
                    "outcome": r.get::<_, Option<String>>(5)?,
                    "opened_at": r.get::<_, String>(6)?,
                    "closed_at": r.get::<_, Option<String>>(7)?,
                }))
            },
        )
        .optional()?;
    Ok(json!({ "incident": head, "transitions": transitions }))
}
