use serde::Deserialize;
use std::collections::{BTreeMap, BTreeSet};
use std::fmt;
use std::path::Path;

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    #[serde(default)]
    pub sensor: SensorSettings,
    #[serde(rename = "target", default)]
    pub targets: Vec<Target>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SensorSettings {
    #[serde(default = "default_interval")]
    pub interval_secs: u64,
    #[serde(default = "default_db")]
    pub db: String,
    #[serde(default = "default_dashboard")]
    pub dashboard: String,
    /// A TCP address outside every target. If it is unreachable too, the
    /// fault is on the probing machine, not the target.
    #[serde(default = "default_canary")]
    pub canary: String,
    #[serde(default = "default_timeout")]
    pub timeout_ms: u64,
    #[serde(default = "default_window")]
    pub baseline_window: usize,
    /// Probe rows older than this are deleted, so a long-running sensor stays small.
    #[serde(default = "default_retention")]
    pub retention_days: u32,
}

impl Default for SensorSettings {
    fn default() -> Self {
        SensorSettings {
            interval_secs: default_interval(),
            db: default_db(),
            dashboard: default_dashboard(),
            canary: default_canary(),
            timeout_ms: default_timeout(),
            baseline_window: default_window(),
            retention_days: default_retention(),
        }
    }
}

fn default_interval() -> u64 {
    30
}
fn default_db() -> String {
    "warden.db".into()
}
fn default_dashboard() -> String {
    "127.0.0.1:7878".into()
}
fn default_canary() -> String {
    "1.1.1.1:443".into()
}
fn default_timeout() -> u64 {
    8000
}
fn default_window() -> usize {
    60
}
fn default_retention() -> u32 {
    7
}
fn default_true() -> bool {
    true
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Kind {
    Http,
    Tcp,
    Heartbeat,
}

impl Kind {
    pub fn as_str(self) -> &'static str {
        match self {
            Kind::Http => "http",
            Kind::Tcp => "tcp",
            Kind::Heartbeat => "heartbeat",
        }
    }
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Target {
    pub name: String,
    pub kind: Kind,
    /// http targets
    pub url: Option<String>,
    /// tcp targets, host:port
    pub address: Option<String>,
    /// heartbeat targets: a file another process touches while it is alive
    pub path: Option<String>,
    pub max_age_secs: Option<u64>,
    pub expected_status: Option<u16>,
    pub latency_budget_ms: u64,
    #[serde(default)]
    pub depends_on: Vec<String>,
    #[serde(default)]
    pub stateless: bool,
    pub restart_url: Option<String>,
    pub restart_cmd: Option<Vec<String>>,
    pub rollback: Option<String>,
    #[serde(default = "default_true")]
    pub enabled: bool,
}

impl Target {
    pub fn endpoint(&self) -> String {
        match self.kind {
            Kind::Http => self.url.clone().unwrap_or_default(),
            Kind::Tcp => self.address.clone().unwrap_or_default(),
            Kind::Heartbeat => self.path.clone().unwrap_or_default(),
        }
    }

    pub fn restart_json(&self) -> Option<String> {
        if let Some(url) = &self.restart_url {
            return Some(serde_json::json!({ "http_post": url }).to_string());
        }
        self.restart_cmd
            .as_ref()
            .map(|cmd| serde_json::json!({ "command": cmd }).to_string())
    }
}

#[derive(Debug)]
pub struct ConfigError {
    pub problems: Vec<String>,
}

impl fmt::Display for ConfigError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        writeln!(f, "warden.toml has {} problem(s):", self.problems.len())?;
        for p in &self.problems {
            writeln!(f, "  - {p}")?;
        }
        Ok(())
    }
}

impl std::error::Error for ConfigError {}

pub fn load(path: &Path) -> Result<Config, ConfigError> {
    let text = std::fs::read_to_string(path).map_err(|e| ConfigError {
        problems: vec![format!(
            "could not read {}: {e}. Copy warden.example.toml to warden.toml to start.",
            path.display()
        )],
    })?;
    parse(&text)
}

pub fn parse(text: &str) -> Result<Config, ConfigError> {
    let config: Config = toml::from_str(text).map_err(|e| ConfigError {
        problems: vec![format!("not valid TOML for Warden: {e}")],
    })?;
    validate(&config)?;
    Ok(config)
}

fn validate(config: &Config) -> Result<(), ConfigError> {
    let mut problems = Vec::new();
    if config.targets.is_empty() {
        problems.push("no [[target]] entries. Add at least one target to watch.".into());
    }
    if config.sensor.interval_secs < 5 {
        problems.push(format!(
            "sensor.interval_secs is {}; use 5 or more so probes do not hammer the targets",
            config.sensor.interval_secs
        ));
    }
    if config.sensor.retention_days == 0 {
        problems.push("sensor.retention_days must be at least 1".into());
    }
    if config.sensor.baseline_window < 10 {
        problems.push("sensor.baseline_window must be at least 10 samples".into());
    }

    let mut names = BTreeSet::new();
    for t in &config.targets {
        let label = if t.name.is_empty() { "<unnamed>" } else { t.name.as_str() };
        if t.name.is_empty()
            || !t
                .name
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_')
        {
            problems.push(format!(
                "target {label:?}: name must be letters, digits, '-' or '_'"
            ));
        }
        if !names.insert(t.name.clone()) {
            problems.push(format!("target {label:?} is defined twice"));
        }
        if t.latency_budget_ms == 0 {
            problems.push(format!("target {label:?}: latency_budget_ms must be above 0"));
        }
        match t.kind {
            Kind::Http => match &t.url {
                Some(u) if u.starts_with("http://") || u.starts_with("https://") => {}
                Some(u) => problems.push(format!(
                    "target {label:?}: url {u:?} must start with http:// or https://"
                )),
                None => problems.push(format!("target {label:?}: kind = \"http\" needs a url")),
            },
            Kind::Tcp => match &t.address {
                Some(a) if a.rsplit_once(':').is_some_and(|(_, p)| p.parse::<u16>().is_ok()) => {}
                _ => problems.push(format!(
                    "target {label:?}: kind = \"tcp\" needs address = \"host:port\""
                )),
            },
            Kind::Heartbeat => {
                if t.path.is_none() || t.max_age_secs.is_none() {
                    problems.push(format!(
                        "target {label:?}: kind = \"heartbeat\" needs path and max_age_secs"
                    ));
                }
            }
        }
        if t.restart_url.is_some() && t.restart_cmd.is_some() {
            problems.push(format!(
                "target {label:?}: set restart_url or restart_cmd, not both"
            ));
        }
        if t.restart_cmd.as_ref().is_some_and(|c| c.is_empty()) {
            problems.push(format!("target {label:?}: restart_cmd is an empty list"));
        }
    }

    for t in &config.targets {
        for dep in &t.depends_on {
            if !names.contains(dep) {
                problems.push(format!(
                    "target {:?} depends on {dep:?}, which is not a target in this file",
                    t.name
                ));
            }
            if dep == &t.name {
                problems.push(format!("target {:?} depends on itself", t.name));
            }
        }
    }

    if problems.is_empty() {
        let edges: BTreeMap<String, Vec<String>> = config
            .targets
            .iter()
            .map(|t| (t.name.clone(), t.depends_on.clone()))
            .collect();
        if let Some(cycle) = crate::graph::find_cycle(&edges) {
            problems.push(format!("dependency cycle: {}", cycle.join(" -> ")));
        }
    }

    if problems.is_empty() { Ok(()) } else { Err(ConfigError { problems }) }
}

#[cfg(test)]
mod tests {
    use super::*;

    const GOOD: &str = r#"
        [sensor]
        interval_secs = 10

        [[target]]
        name = "api"
        kind = "http"
        url = "http://127.0.0.1:9000/"
        expected_status = 200
        latency_budget_ms = 500
        depends_on = ["db"]

        [[target]]
        name = "db"
        kind = "tcp"
        address = "127.0.0.1:5432"
        latency_budget_ms = 100
    "#;

    #[test]
    fn parses_a_good_file() {
        let c = parse(GOOD).unwrap();
        assert_eq!(c.targets.len(), 2);
        assert_eq!(c.sensor.interval_secs, 10);
        assert_eq!(c.sensor.dashboard, "127.0.0.1:7878");
    }

    #[test]
    fn names_every_problem_at_once() {
        let bad = r#"
            [[target]]
            name = "api"
            kind = "http"
            url = "ftp://nope"
            latency_budget_ms = 0
            depends_on = ["ghost"]
        "#;
        let err = parse(bad).unwrap_err();
        let text = err.to_string();
        assert!(text.contains("must start with http"), "{text}");
        assert!(text.contains("latency_budget_ms must be above 0"), "{text}");
        assert!(text.contains("\"ghost\", which is not a target"), "{text}");
    }

    #[test]
    fn rejects_cycles() {
        let bad = r#"
            [[target]]
            name = "a"
            kind = "tcp"
            address = "127.0.0.1:1"
            latency_budget_ms = 10
            depends_on = ["b"]
            [[target]]
            name = "b"
            kind = "tcp"
            address = "127.0.0.1:2"
            latency_budget_ms = 10
            depends_on = ["a"]
        "#;
        let text = parse(bad).unwrap_err().to_string();
        assert!(text.contains("dependency cycle"), "{text}");
    }

    #[test]
    fn rejects_unknown_keys() {
        let bad = GOOD.replace("expected_status = 200", "expected_stauts = 200");
        let text = parse(&bad).unwrap_err().to_string();
        assert!(text.contains("expected_stauts"), "{text}");
    }
}
