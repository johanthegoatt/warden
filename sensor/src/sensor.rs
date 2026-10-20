//! The probe loop for one target: probe, score drift, and on failure verify
//! through the second path, the canary and the dependencies before raising
//! anything.

use crate::baseline::Baseline;
use crate::config::{Config, Target};
use crate::graph::{self, Edges};
use crate::probe::{self, Outcome};
use crate::store;
use crate::verify::{self, Fault, Observations};
use serde_json::json;
use std::path::PathBuf;
use std::time::{Duration, Instant};

/// Drift alerts look this many probes ahead of the trend line.
const HORIZON_PROBES: f64 = 10.0;
/// A fault in Warden's own probe path is reported only after this many rounds in a row.
const PROBE_STRIKES: u32 = 3;

#[derive(Debug, Clone, PartialEq)]
enum Health {
    Healthy,
    Failing(String),
}

pub struct Loop {
    pub target: Target,
    pub edges: Edges,
    pub db: PathBuf,
    pub canary: String,
    pub timeout: Duration,
    pub interval: Duration,
    pub window: usize,
}

impl Loop {
    pub fn from_config(config: &Config, target: &Target, db: PathBuf) -> Self {
        Loop {
            target: target.clone(),
            edges: config
                .targets
                .iter()
                .map(|t| (t.name.clone(), t.depends_on.clone()))
                .collect(),
            db,
            canary: config.sensor.canary.clone(),
            timeout: Duration::from_millis(config.sensor.timeout_ms),
            interval: Duration::from_secs(config.sensor.interval_secs),
            window: config.sensor.baseline_window,
        }
    }

    /// Runs forever. A panic or a database error ends the thread and the
    /// supervisor starts a new one, which reloads the baseline from SQLite.
    pub fn run(&self, rounds: Option<usize>) {
        let conn = store::open(&self.db).expect("cannot open the Warden database");
        let mut baseline = Baseline::new(self.window);
        for l in store::recent_latencies(&conn, &self.target.name, self.window)
            .expect("cannot read past probes")
        {
            baseline.push(l);
        }
        let mut health = match store::open_fault(&conn, &self.target.name).expect("cannot read past events") {
            Some(key) => Health::Failing(key),
            None => Health::Healthy,
        };
        let mut drifting = false;
        let mut strikes = 0;
        let mut done = 0;
        loop {
            let started = Instant::now();
            self.round(&conn, &mut baseline, &mut health, &mut drifting, &mut strikes)
                .expect("database write failed during a probe round");
            done += 1;
            if rounds.is_some_and(|r| done >= r) {
                return;
            }
            std::thread::sleep(self.interval.saturating_sub(started.elapsed()));
        }
    }

    fn round(
        &self,
        conn: &rusqlite::Connection,
        baseline: &mut Baseline,
        health: &mut Health,
        drifting: &mut bool,
        strikes: &mut u32,
    ) -> rusqlite::Result<()> {
        let t = &self.target;
        let primary = probe::primary(t, self.timeout);
        let primary_id = store::record_probe(conn, &t.name, "primary", &primary)?;

        if primary.ok {
            *strikes = 0;
            if let Some(l) = primary.latency_ms {
                baseline.push(l);
            }
            if let Health::Failing(previous) = health {
                store::record_event(
                    conn,
                    &t.name,
                    "recovered",
                    "none",
                    None,
                    None,
                    &json!({ "probe_id": primary_id, "previous_fault": previous }),
                )?;
                *health = Health::Healthy;
            }
            if let Some(d) = baseline.assess(t.latency_budget_ms as f64, HORIZON_PROBES) {
                if d.alert && !*drifting {
                    let recent: Vec<f64> = baseline.samples().copied().collect();
                    store::record_event(
                        conn,
                        &t.name,
                        "drift",
                        "target",
                        Some(&t.name),
                        Some(d.z),
                        &json!({ "probe_id": primary_id, "drift": d, "latencies": recent }),
                    )?;
                }
                *drifting = d.alert;
            }
            return Ok(());
        }

        let secondary = probe::secondary(t, self.timeout);
        let secondary_id = store::record_probe(conn, &t.name, "secondary", &secondary)?;
        let canary = probe::canary(&self.canary, self.timeout);
        let canary_id = store::record_probe(conn, &t.name, "canary", &canary)?;
        let mut deps = Vec::new();
        for dep in graph::upstream(&self.edges, &t.name) {
            // A dependency with no probes yet counts as healthy; it is not evidence.
            let healthy = store::latest_ok(conn, &dep)?.unwrap_or(true);
            deps.push((dep, healthy));
        }
        let fault = verify::classify(&Observations {
            target: &t.name,
            primary: &primary,
            secondary: &secondary,
            canary: &canary,
            dependencies: &deps,
        });
        if fault == Fault::None {
            return Ok(());
        }
        if fault.kind() == "probe" {
            // One cut-off handshake is noise; the same path failing three rounds running is a finding.
            *strikes += 1;
            if *strikes < PROBE_STRIKES {
                return Ok(());
            }
        } else {
            *strikes = 0;
        }
        let key = format!("{}:{}", fault.kind(), fault.culprit().unwrap_or(""));
        if *health == Health::Failing(key.clone()) {
            return Ok(());
        }
        store::record_event(
            conn,
            &t.name,
            "failure",
            fault.kind(),
            fault.culprit(),
            None,
            &json!({
                "probe_ids": { "primary": primary_id, "secondary": secondary_id, "canary": canary_id },
                "primary": primary,
                "secondary": secondary,
                "canary": canary,
                "dependencies": deps.iter().map(|(n, h)| json!({ "name": n, "healthy": h })).collect::<Vec<_>>(),
            }),
        )?;
        *health = Health::Failing(key);
        Ok(())
    }
}

pub fn outcome_summary(o: &Outcome) -> String {
    match (&o.error, o.status, o.latency_ms) {
        (Some(e), _, _) => e.clone(),
        (None, Some(s), Some(l)) => format!("{s} in {l:.0} ms"),
        (None, None, Some(l)) => format!("reachable in {l:.0} ms"),
        _ => "no result".into(),
    }
}
