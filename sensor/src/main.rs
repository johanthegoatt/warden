mod baseline;
mod config;
mod dashboard;
mod fake;
mod graph;
mod probe;
mod sensor;
mod store;
mod supervisor;
mod verify;

use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::sync::{Arc, Mutex};
use std::time::Duration;

const USAGE: &str = "warden-sensor

  run   [--config warden.toml] [--once] [--no-dashboard]
        Probe every target forever and serve the dashboard.
  check [--config warden.toml]
        Validate the config and probe each target once through both paths.
  fake  [--port 8099] [--control 8098]
        Start a target that fails on command, for tests and demos.
";

fn flag(args: &[String], name: &str) -> bool {
    args.iter().any(|a| a == name)
}

fn value(args: &[String], name: &str) -> Option<String> {
    args.iter()
        .position(|a| a == name)
        .and_then(|i| args.get(i + 1).cloned())
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let config_path = PathBuf::from(value(&args, "--config").unwrap_or_else(|| "warden.toml".into()));
    match args.first().map(String::as_str) {
        Some("run") => run(&config_path, flag(&args, "--once"), flag(&args, "--no-dashboard")),
        Some("check") => check(&config_path),
        Some("fake") => {
            let port = value(&args, "--port").and_then(|p| p.parse().ok()).unwrap_or(8099);
            let control = value(&args, "--control").and_then(|p| p.parse().ok()).unwrap_or(8098);
            match fake::start(port, control) {
                Ok(f) => {
                    println!("fake target on http://{}  control on http://{}", f.service, f.control);
                    loop {
                        std::thread::park();
                    }
                }
                Err(e) => {
                    eprintln!("could not bind the fake target ports: {e}");
                    ExitCode::FAILURE
                }
            }
        }
        _ => {
            eprint!("{USAGE}");
            ExitCode::from(2)
        }
    }
}

fn load(config_path: &Path) -> Result<config::Config, ExitCode> {
    let mut cfg = config::load(config_path).map_err(|e| {
        eprint!("{e}");
        ExitCode::from(2)
    })?;
    // Heartbeat paths, like the database, are relative to the config file.
    let base = config_path.parent().unwrap_or(Path::new("."));
    for t in &mut cfg.targets {
        if let Some(p) = &t.path {
            if PathBuf::from(p).is_relative() {
                t.path = Some(base.join(p).to_string_lossy().into_owned());
            }
        }
    }
    Ok(cfg)
}

/// The database path in the config is relative to the config file, not the shell.
fn db_path(config_path: &Path, cfg: &config::Config) -> PathBuf {
    let p = PathBuf::from(&cfg.sensor.db);
    if p.is_absolute() {
        p
    } else {
        config_path.parent().unwrap_or(Path::new(".")).join(p)
    }
}

fn check(config_path: &Path) -> ExitCode {
    let cfg = match load(config_path) {
        Ok(c) => c,
        Err(code) => return code,
    };
    let timeout = Duration::from_millis(cfg.sensor.timeout_ms);
    println!("{} target(s) in {}", cfg.targets.len(), config_path.display());
    for t in cfg.targets.iter().filter(|t| t.enabled) {
        let p = probe::primary(t, timeout);
        let s = probe::secondary(t, timeout);
        println!(
            "  {:<22} primary: {:<40} secondary: {}",
            t.name,
            sensor::outcome_summary(&p),
            sensor::outcome_summary(&s)
        );
    }
    ExitCode::SUCCESS
}

fn run(config_path: &Path, once: bool, no_dashboard: bool) -> ExitCode {
    let cfg = match load(config_path) {
        Ok(c) => c,
        Err(code) => return code,
    };
    let db = db_path(config_path, &cfg);
    let mut conn = match store::open(&db) {
        Ok(c) => c,
        Err(e) => {
            eprintln!("could not open {}: {e}", db.display());
            return ExitCode::FAILURE;
        }
    };
    if let Err(e) = store::sync_targets(&mut conn, &cfg.targets) {
        eprintln!("could not write targets to {}: {e}", db.display());
        return ExitCode::FAILURE;
    }
    let enabled: Vec<_> = cfg.targets.iter().filter(|t| t.enabled).cloned().collect();

    if once {
        // Dependencies first, so each target's verification sees fresh upstream results.
        let edges: graph::Edges = cfg.targets.iter().map(|t| (t.name.clone(), t.depends_on.clone())).collect();
        let mut ordered = enabled.clone();
        // A dependency's upstream set is a strict subset of its dependent's, so it sorts first.
        ordered.sort_by_key(|t| graph::upstream(&edges, &t.name).len());
        for t in &ordered {
            sensor::Loop::from_config(&cfg, t, db.clone()).run(Some(1));
        }
        let _ = store::beat(&conn, "sensor", &serde_json::json!({ "mode": "once" }).to_string());
        println!("probed {} target(s) once into {}", ordered.len(), db.display());
        return ExitCode::SUCCESS;
    }

    if !no_dashboard {
        let addr = cfg.sensor.dashboard.clone();
        let settings = dashboard::Settings {
            db: db.clone(),
            token: std::env::var("WARDEN_TOKEN").ok().filter(|t| !t.is_empty()),
        };
        std::thread::spawn(move || {
            if let Err(e) = dashboard::serve(&addr, settings) {
                eprintln!("dashboard could not bind {addr}: {e}");
            }
        });
        println!("dashboard on http://{}", cfg.sensor.dashboard);
    }

    let log_conn = Arc::new(Mutex::new(conn));
    let lc = log_conn.clone();
    let mut sup = supervisor::Supervisor::new(
        Duration::from_secs(1),
        Box::new(move |task, reason, n| {
            eprintln!("supervisor: restarting {task} (restart {n}): {reason}");
            if let Ok(c) = lc.lock() {
                let _ = store::record_restart(&c, task, reason, n);
            }
        }),
    );
    for (i, t) in enabled.iter().enumerate() {
        let lp = Arc::new(sensor::Loop::from_config(&cfg, t, db.clone()));
        let stagger = Duration::from_millis(400 * i as u64);
        sup.spawn(
            &t.name,
            Arc::new(move || {
                std::thread::sleep(stagger);
                lp.run(None)
            }),
        );
    }
    println!("watching {} target(s) every {}s", enabled.len(), cfg.sensor.interval_secs);
    let mut last_prune: Option<std::time::Instant> = None;
    loop {
        sup.tick();
        if let Ok(c) = log_conn.lock() {
            let _ = store::beat(&c, "sensor", &serde_json::json!({ "targets": enabled.len() }).to_string());
            if last_prune.is_none_or(|t| t.elapsed() >= Duration::from_secs(3600)) {
                match store::prune(&c, cfg.sensor.retention_days) {
                    Ok(n) if n > 0 => println!("pruned {n} probe(s) older than {} days", cfg.sensor.retention_days),
                    Ok(_) => {}
                    Err(e) => eprintln!("pruning old probes failed: {e}"),
                }
                last_prune = Some(std::time::Instant::now());
            }
        }
        std::thread::sleep(Duration::from_secs(1));
    }
}
