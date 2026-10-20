//! Rule 2: a failed probe is not an outage.
//!
//! Before an incident is raised the sensor has three more observations: the
//! independent secondary path, a canary outside every target, and the current
//! health of each upstream dependency. This module turns them into a fault
//! location. It is pure so every branch is tested without a network.

use crate::probe::Outcome;
use serde::Serialize;

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(tag = "fault", content = "culprit", rename_all = "lowercase")]
pub enum Fault {
    /// Nothing is wrong. Healthy is a valid answer.
    None,
    /// The probing machine or one probe path is broken; the target was reachable or untested.
    Probe(String),
    /// The target is fine on its own, but something it needs is down.
    Dependency(String),
    Target(String),
}

impl Fault {
    pub fn kind(&self) -> &'static str {
        match self {
            Fault::None => "none",
            Fault::Probe(_) => "probe",
            Fault::Dependency(_) => "dependency",
            Fault::Target(_) => "target",
        }
    }
    pub fn culprit(&self) -> Option<&str> {
        match self {
            Fault::None => None,
            Fault::Probe(c) | Fault::Dependency(c) | Fault::Target(c) => Some(c),
        }
    }
}

pub struct Observations<'a> {
    pub target: &'a str,
    pub primary: &'a Outcome,
    pub secondary: &'a Outcome,
    pub canary: &'a Outcome,
    /// (dependency name, currently healthy), nearest first
    pub dependencies: &'a [(String, bool)],
}

pub fn classify(o: &Observations) -> Fault {
    if o.primary.ok {
        return Fault::None;
    }
    if o.secondary.ok {
        // The target answered correctly to an independent client.
        return Fault::Probe("primary probe path".into());
    }
    if !o.canary.ok {
        return Fault::Probe("probe host network".into());
    }
    if o.primary.probe_error && o.secondary.probe_error {
        return Fault::Probe("both probe paths failed to run".into());
    }
    if let Some((dep, _)) = o.dependencies.iter().find(|(_, healthy)| !healthy) {
        return Fault::Dependency(dep.clone());
    }
    Fault::Target(o.target.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn up() -> Outcome {
        Outcome {
            ok: true,
            status: Some(200),
            latency_ms: Some(80.0),
            error: None,
            probe_error: false,
        }
    }
    fn down() -> Outcome {
        Outcome {
            ok: false,
            status: None,
            latency_ms: Some(8000.0),
            error: Some("timed out".into()),
            probe_error: false,
        }
    }
    fn broken() -> Outcome {
        Outcome {
            ok: false,
            status: None,
            latency_ms: None,
            error: Some("tls setup on this machine".into()),
            probe_error: true,
        }
    }
    fn obs<'a>(
        p: &'a Outcome,
        s: &'a Outcome,
        c: &'a Outcome,
        deps: &'a [(String, bool)],
    ) -> Observations<'a> {
        Observations {
            target: "site",
            primary: p,
            secondary: s,
            canary: c,
            dependencies: deps,
        }
    }

    #[test]
    fn healthy_primary_means_no_fault() {
        assert_eq!(classify(&obs(&up(), &down(), &down(), &[])), Fault::None);
    }

    #[test]
    fn primary_fails_but_independent_path_succeeds_is_a_probe_fault() {
        let f = classify(&obs(&down(), &up(), &up(), &[]));
        assert_eq!(f.kind(), "probe");
    }

    #[test]
    fn missing_local_tool_is_never_reported_as_an_outage() {
        // The Causely failure: an agent's own broken tool became a "service down" story.
        let f = classify(&obs(&broken(), &broken(), &up(), &[]));
        assert_eq!(f.kind(), "probe");
        let f = classify(&obs(&broken(), &up(), &up(), &[]));
        assert_eq!(f.kind(), "probe");
    }

    #[test]
    fn both_paths_down_and_canary_down_blames_the_probe_host() {
        let f = classify(&obs(&down(), &down(), &down(), &[]));
        assert_eq!(f, Fault::Probe("probe host network".into()));
    }

    #[test]
    fn unhealthy_dependency_is_named() {
        let deps = vec![("site".to_string(), true), ("discord-api".to_string(), false)];
        let f = classify(&obs(&down(), &down(), &up(), &deps));
        assert_eq!(f, Fault::Dependency("discord-api".into()));
    }

    #[test]
    fn confirmed_by_both_paths_with_healthy_deps_is_the_target() {
        let deps = vec![("discord-api".to_string(), true)];
        let f = classify(&obs(&down(), &down(), &up(), &deps));
        assert_eq!(f, Fault::Target("site".into()));
    }
}
