use crate::config::Target;
use crate::probe::Outcome;
use rusqlite::{Connection, params};
use std::path::Path;
use std::time::Duration;

pub const SCHEMA: &str = include_str!("../../schema.sql");
pub const NOW: &str = "strftime('%Y-%m-%dT%H:%M:%fZ','now')";

/// Every thread opens its own connection; WAL lets the brain read while the
/// sensor writes.
pub fn open(path: &Path) -> rusqlite::Result<Connection> {
    let conn = Connection::open(path)?;
    conn.busy_timeout(Duration::from_secs(5))?;
    conn.pragma_update(None, "journal_mode", "WAL")?;
    conn.pragma_update(None, "foreign_keys", "ON")?;
    conn.execute_batch(SCHEMA)?;
    Ok(conn)
}

pub fn sync_targets(conn: &mut Connection, targets: &[Target]) -> rusqlite::Result<()> {
    let tx = conn.transaction()?;
    {
        let names: Vec<String> = targets.iter().map(|t| t.name.clone()).collect();
        let keep = serde_json::to_string(&names).unwrap_or_else(|_| "[]".into());
        tx.execute(
            "DELETE FROM targets WHERE name NOT IN (SELECT value FROM json_each(?1))",
            params![keep],
        )?;
        let mut up = tx.prepare(&format!(
            "INSERT INTO targets (name, kind, endpoint, expected_status, latency_budget_ms, depends_on, stateless, restart, rollback, max_age_secs, updated_at)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, {NOW})
             ON CONFLICT(name) DO UPDATE SET kind = excluded.kind, endpoint = excluded.endpoint,
               expected_status = excluded.expected_status, latency_budget_ms = excluded.latency_budget_ms,
               depends_on = excluded.depends_on, stateless = excluded.stateless,
               restart = excluded.restart, rollback = excluded.rollback,
               max_age_secs = excluded.max_age_secs, updated_at = excluded.updated_at"
        ))?;
        for t in targets.iter().filter(|t| t.enabled) {
            up.execute(params![
                t.name,
                t.kind.as_str(),
                t.endpoint(),
                t.expected_status,
                t.latency_budget_ms as i64,
                serde_json::to_string(&t.depends_on).unwrap_or_else(|_| "[]".into()),
                t.stateless as i64,
                t.restart_json(),
                t.rollback,
                t.max_age_secs.map(|s| s as i64),
            ])?;
        }
    }
    tx.commit()
}

pub fn record_probe(
    conn: &Connection,
    target: &str,
    path: &str,
    o: &Outcome,
) -> rusqlite::Result<i64> {
    conn.execute(
        &format!(
            "INSERT INTO probes (ts, target, path, ok, status, latency_ms, error) VALUES ({NOW}, ?1, ?2, ?3, ?4, ?5, ?6)"
        ),
        params![target, path, o.ok as i64, o.status, o.latency_ms, o.error],
    )?;
    Ok(conn.last_insert_rowid())
}

pub fn record_event(
    conn: &Connection,
    target: &str,
    kind: &str,
    fault: &str,
    culprit: Option<&str>,
    score: Option<f64>,
    detail: &serde_json::Value,
) -> rusqlite::Result<i64> {
    conn.execute(
        &format!(
            "INSERT INTO events (ts, target, kind, fault, culprit, score, detail) VALUES ({NOW}, ?1, ?2, ?3, ?4, ?5, ?6)"
        ),
        params![target, kind, fault, culprit, score, detail.to_string()],
    )?;
    Ok(conn.last_insert_rowid())
}

pub fn record_restart(conn: &Connection, task: &str, reason: &str, restarts: u32) -> rusqlite::Result<()> {
    conn.execute(
        &format!("INSERT INTO supervisor (ts, task, reason, restarts) VALUES ({NOW}, ?1, ?2, ?3)"),
        params![task, reason, restarts],
    )?;
    Ok(())
}

pub fn beat(conn: &Connection, component: &str, note: &str) -> rusqlite::Result<()> {
    conn.execute(
        &format!(
            "INSERT INTO heartbeat (component, ts, note) VALUES (?1, {NOW}, ?2)
             ON CONFLICT(component) DO UPDATE SET ts = excluded.ts, note = excluded.note"
        ),
        params![component, note],
    )?;
    Ok(())
}

/// Latest primary result per target, used to judge dependencies.
pub fn latest_ok(conn: &Connection, target: &str) -> rusqlite::Result<Option<bool>> {
    let mut stmt = conn.prepare_cached(
        "SELECT ok FROM probes WHERE target = ?1 AND path = 'primary' ORDER BY id DESC LIMIT 1",
    )?;
    let mut rows = stmt.query(params![target])?;
    Ok(match rows.next()? {
        Some(r) => Some(r.get::<_, i64>(0)? == 1),
        None => None,
    })
}

/// Recent successful latencies, oldest first, so a restarted sensor keeps its baseline.
pub fn recent_latencies(conn: &Connection, target: &str, limit: usize) -> rusqlite::Result<Vec<f64>> {
    let mut stmt = conn.prepare(
        "SELECT latency_ms FROM (SELECT id, latency_ms FROM probes
           WHERE target = ?1 AND path = 'primary' AND ok = 1 AND latency_ms IS NOT NULL
           ORDER BY id DESC LIMIT ?2) ORDER BY id",
    )?;
    let rows = stmt.query_map(params![target, limit as i64], |r| r.get(0))?;
    rows.collect()
}
