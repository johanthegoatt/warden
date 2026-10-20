//! Each probe loop runs on its own thread. If one panics or returns, the
//! supervisor notices on its next tick and starts a fresh copy after a backoff
//! that doubles up to a minute.

use std::sync::Arc;
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

type Factory = Arc<dyn Fn() + Send + Sync>;
type OnRestart = Box<dyn FnMut(&str, &str, u32) + Send>;

struct Task {
    name: String,
    factory: Factory,
    handle: Option<JoinHandle<()>>,
    restarts: u32,
    restart_at: Option<Instant>,
    pending_reason: String,
}

pub struct Supervisor {
    tasks: Vec<Task>,
    base_backoff: Duration,
    on_restart: OnRestart,
}

impl Supervisor {
    pub fn new(base_backoff: Duration, on_restart: OnRestart) -> Self {
        Supervisor {
            tasks: Vec::new(),
            base_backoff,
            on_restart,
        }
    }

    pub fn spawn(&mut self, name: &str, factory: Factory) {
        let handle = start(name, &factory);
        self.tasks.push(Task {
            name: name.to_string(),
            factory,
            handle: Some(handle),
            restarts: 0,
            restart_at: None,
            pending_reason: String::new(),
        });
    }

    /// Returns how many tasks were restarted on this tick.
    pub fn tick(&mut self) -> usize {
        let now = Instant::now();
        let mut restarted = 0;
        for task in &mut self.tasks {
            if task.handle.as_ref().is_some_and(|h| h.is_finished()) {
                let reason = match task.handle.take().map(|h| h.join()) {
                    Some(Err(payload)) => format!("panicked: {}", panic_message(&payload)),
                    _ => "returned unexpectedly".to_string(),
                };
                let backoff = self
                    .base_backoff
                    .saturating_mul(1u32 << task.restarts.min(6))
                    .min(Duration::from_secs(60));
                task.restart_at = Some(now + backoff);
                task.pending_reason = reason;
            }
            if task.handle.is_none() && task.restart_at.is_some_and(|t| now >= t) {
                task.restarts += 1;
                (self.on_restart)(&task.name, &task.pending_reason, task.restarts);
                task.handle = Some(start(&task.name, &task.factory));
                task.restart_at = None;
                restarted += 1;
            }
        }
        restarted
    }

    #[cfg(test)]
    pub fn restarts(&self, name: &str) -> u32 {
        self.tasks
            .iter()
            .find(|t| t.name == name)
            .map(|t| t.restarts)
            .unwrap_or(0)
    }
}

fn start(name: &str, factory: &Factory) -> JoinHandle<()> {
    let f = factory.clone();
    std::thread::Builder::new()
        .name(format!("probe:{name}"))
        .spawn(move || f())
        .expect("the OS refused to start a thread")
}

fn panic_message(payload: &Box<dyn std::any::Any + Send>) -> String {
    if let Some(s) = payload.downcast_ref::<&str>() {
        (*s).to_string()
    } else if let Some(s) = payload.downcast_ref::<String>() {
        s.clone()
    } else {
        "unknown panic".to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;
    use std::sync::atomic::{AtomicUsize, Ordering};

    #[test]
    fn restarts_a_task_that_panics() {
        let runs = Arc::new(AtomicUsize::new(0));
        let log = Arc::new(Mutex::new(Vec::new()));
        let log2 = log.clone();
        let mut sup = Supervisor::new(
            Duration::ZERO,
            Box::new(move |name, reason, n| {
                log2.lock().unwrap().push(format!("{name}|{reason}|{n}"))
            }),
        );
        let r = runs.clone();
        sup.spawn(
            "flaky",
            Arc::new(move || {
                if r.fetch_add(1, Ordering::SeqCst) == 0 {
                    panic!("probe blew up");
                }
                std::thread::sleep(Duration::from_secs(2));
            }),
        );
        let deadline = Instant::now() + Duration::from_secs(5);
        while sup.restarts("flaky") == 0 && Instant::now() < deadline {
            sup.tick();
            std::thread::sleep(Duration::from_millis(10));
        }
        assert_eq!(sup.restarts("flaky"), 1);
        assert_eq!(runs.load(Ordering::SeqCst), 2);
        let log = log.lock().unwrap();
        assert!(log[0].starts_with("flaky|panicked: probe blew up|1"), "{log:?}");
    }

    #[test]
    fn healthy_tasks_are_left_alone() {
        let mut sup = Supervisor::new(Duration::ZERO, Box::new(|_, _, _| {}));
        sup.spawn("steady", Arc::new(|| std::thread::sleep(Duration::from_millis(300))));
        assert_eq!(sup.tick(), 0);
        assert_eq!(sup.restarts("steady"), 0);
    }
}
