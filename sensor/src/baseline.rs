//! Learns each target's normal latency and scores how far the latest samples
//! have moved from it. No model involved: a rolling window, a z-score and a
//! least-squares trend line that predicts when the latency budget will break.

use serde::Serialize;
use std::collections::VecDeque;

const RECENT: usize = 5;
const TREND: usize = 10;
const MIN_BASE: usize = 10;
pub const Z_ALERT: f64 = 3.0;

#[derive(Debug, Clone)]
pub struct Baseline {
    cap: usize,
    samples: VecDeque<f64>,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Drift {
    pub base_mean_ms: f64,
    pub base_std_ms: f64,
    pub recent_mean_ms: f64,
    pub z: f64,
    pub slope_ms_per_probe: f64,
    /// Probes until the trend line crosses the budget, when it is heading there.
    pub breach_in_probes: Option<f64>,
    pub over_budget: bool,
    pub alert: bool,
    pub reason: String,
}

impl Baseline {
    pub fn new(cap: usize) -> Self {
        Baseline {
            cap: cap.max(MIN_BASE + RECENT),
            samples: VecDeque::new(),
        }
    }

    pub fn push(&mut self, latency_ms: f64) {
        if self.samples.len() == self.cap {
            self.samples.pop_front();
        }
        self.samples.push_back(latency_ms);
    }

    #[cfg(test)]
    pub fn len(&self) -> usize {
        self.samples.len()
    }

    pub fn samples(&self) -> impl Iterator<Item = &f64> {
        self.samples.iter()
    }

    /// None until there are enough samples to say what normal looks like.
    pub fn assess(&self, budget_ms: f64, horizon_probes: f64) -> Option<Drift> {
        if self.samples.len() < MIN_BASE + RECENT {
            return None;
        }
        let split = self.samples.len() - RECENT;
        let base: Vec<f64> = self.samples.iter().take(split).copied().collect();
        let recent: Vec<f64> = self.samples.iter().skip(split).copied().collect();
        let (bm, bs) = mean_std(&base);
        let (rm, _) = mean_std(&recent);
        // A perfectly flat baseline would make any wobble look infinite.
        let floor = (bm * 0.05).max(1.0);
        let z = (rm - bm) / bs.max(floor);

        let tail: Vec<f64> = self
            .samples
            .iter()
            .skip(self.samples.len().saturating_sub(TREND))
            .copied()
            .collect();
        let (slope, last_fit) = trend(&tail);
        let breach_in = if slope > 0.0 && last_fit < budget_ms {
            Some(((budget_ms - last_fit) / slope * 10.0).round() / 10.0)
        } else {
            None
        };
        let over_budget = rm > budget_ms;
        let predicted = breach_in.is_some_and(|n| n <= horizon_probes) && z >= 1.5;
        let shifted = z >= Z_ALERT && rm > bm * 1.25;

        let reason = if over_budget {
            format!("recent mean {rm:.0} ms is over the {budget_ms:.0} ms budget")
        } else if predicted {
            format!(
                "latency rising {slope:.1} ms per probe; budget of {budget_ms:.0} ms crossed in about {:.0} probes",
                breach_in.unwrap_or_default()
            )
        } else if shifted {
            format!("recent mean {rm:.0} ms is {z:.1} standard deviations above the usual {bm:.0} ms")
        } else {
            "within normal range".into()
        };

        Some(Drift {
            base_mean_ms: round1(bm),
            base_std_ms: round1(bs),
            recent_mean_ms: round1(rm),
            z: round1(z),
            slope_ms_per_probe: round1(slope),
            breach_in_probes: breach_in,
            over_budget,
            alert: over_budget || predicted || shifted,
            reason,
        })
    }
}

fn round1(x: f64) -> f64 {
    (x * 10.0).round() / 10.0
}

fn mean_std(xs: &[f64]) -> (f64, f64) {
    let n = xs.len() as f64;
    let mean = xs.iter().sum::<f64>() / n;
    let var = xs.iter().map(|x| (x - mean).powi(2)).sum::<f64>() / (n - 1.0).max(1.0);
    (mean, var.sqrt())
}

/// Least-squares slope per sample and the fitted value at the last sample.
fn trend(ys: &[f64]) -> (f64, f64) {
    let n = ys.len() as f64;
    if ys.len() < 3 {
        return (0.0, ys.last().copied().unwrap_or(0.0));
    }
    let mx = (n - 1.0) / 2.0;
    let my = ys.iter().sum::<f64>() / n;
    let mut num = 0.0;
    let mut den = 0.0;
    for (i, y) in ys.iter().enumerate() {
        let dx = i as f64 - mx;
        num += dx * (y - my);
        den += dx * dx;
    }
    let slope = num / den;
    (slope, my + slope * (n - 1.0 - mx))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn steady(b: &mut Baseline, n: usize) {
        for i in 0..n {
            b.push(100.0 + (i % 5) as f64 * 2.0);
        }
    }

    #[test]
    fn needs_enough_samples() {
        let mut b = Baseline::new(60);
        steady(&mut b, 14);
        assert!(b.assess(500.0, 10.0).is_none());
        b.push(101.0);
        assert!(b.assess(500.0, 10.0).is_some());
    }

    #[test]
    fn steady_latency_is_not_drift() {
        let mut b = Baseline::new(60);
        steady(&mut b, 40);
        let d = b.assess(500.0, 10.0).unwrap();
        assert!(!d.alert, "{d:?}");
        assert_eq!(d.reason, "within normal range");
    }

    #[test]
    fn a_jump_is_flagged() {
        let mut b = Baseline::new(60);
        steady(&mut b, 40);
        for _ in 0..5 {
            b.push(260.0);
        }
        let d = b.assess(500.0, 10.0).unwrap();
        assert!(d.alert);
        assert!(d.z >= Z_ALERT);
    }

    #[test]
    fn a_climb_toward_the_budget_is_predicted_before_it_breaks() {
        let mut b = Baseline::new(60);
        steady(&mut b, 30);
        for i in 0..12 {
            b.push(110.0 + i as f64 * 25.0);
        }
        let d = b.assess(500.0, 10.0).unwrap();
        assert!(!d.over_budget, "{d:?}");
        assert!(d.alert, "{d:?}");
        assert!(d.breach_in_probes.unwrap() <= 10.0, "{d:?}");
        assert!(d.reason.contains("crossed in about"));
    }

    #[test]
    fn window_is_bounded() {
        let mut b = Baseline::new(20);
        steady(&mut b, 100);
        assert_eq!(b.len(), 20);
    }
}
