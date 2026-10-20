//! A target that fails on command, for tests and for producing sample reports.
//!
//! The service port answers GET requests according to the current mode. The
//! control port changes the mode: POST /mode/<name> and POST /restart.

use std::io::{Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::sync::{Arc, Mutex};
use std::time::Duration;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Mode {
    Healthy,
    /// 500 to every client
    Error,
    /// accepts then closes without answering
    Down,
    /// latency rises 40 ms on every request
    Climbing,
    /// 500 only to the primary probe's user agent: a broken probe path, healthy service
    PrimaryOnly,
}

impl Mode {
    pub fn parse(s: &str) -> Option<Mode> {
        Some(match s {
            "healthy" => Mode::Healthy,
            "error" => Mode::Error,
            "down" => Mode::Down,
            "climbing" => Mode::Climbing,
            "primary-only" => Mode::PrimaryOnly,
            _ => return None,
        })
    }
    fn name(self) -> &'static str {
        match self {
            Mode::Healthy => "healthy",
            Mode::Error => "error",
            Mode::Down => "down",
            Mode::Climbing => "climbing",
            Mode::PrimaryOnly => "primary-only",
        }
    }
}

struct State {
    mode: Mode,
    requests: u64,
    restarts: u64,
}

pub struct Fake {
    pub service: SocketAddr,
    pub control: SocketAddr,
}

pub fn start(service_port: u16, control_port: u16) -> std::io::Result<Fake> {
    let service = TcpListener::bind(("127.0.0.1", service_port))?;
    let control = TcpListener::bind(("127.0.0.1", control_port))?;
    let fake = Fake {
        service: service.local_addr()?,
        control: control.local_addr()?,
    };
    let state = Arc::new(Mutex::new(State {
        mode: Mode::Healthy,
        requests: 0,
        restarts: 0,
    }));
    let s1 = state.clone();
    std::thread::spawn(move || {
        for stream in service.incoming().flatten() {
            let s = s1.clone();
            std::thread::spawn(move || serve(stream, &s));
        }
    });
    std::thread::spawn(move || {
        for stream in control.incoming().flatten() {
            let s = state.clone();
            std::thread::spawn(move || steer(stream, &s));
        }
    });
    Ok(fake)
}

fn read_head(stream: &mut TcpStream) -> String {
    let _ = stream.set_read_timeout(Some(Duration::from_secs(5)));
    let mut buf = Vec::new();
    let mut chunk = [0u8; 512];
    while !buf.windows(4).any(|w| w == b"\r\n\r\n") && buf.len() < 16384 {
        match stream.read(&mut chunk) {
            Ok(0) | Err(_) => break,
            Ok(n) => buf.extend_from_slice(&chunk[..n]),
        }
    }
    String::from_utf8_lossy(&buf).into_owned()
}

fn reply(stream: &mut TcpStream, status: &str, body: &str) {
    let _ = write!(
        stream,
        "HTTP/1.1 {status}\r\nContent-Type: text/plain\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    );
}

fn serve(mut stream: TcpStream, state: &Mutex<State>) {
    let head = read_head(&mut stream);
    let primary = head
        .lines()
        .any(|l| l.to_ascii_lowercase().starts_with("user-agent: warden-primary"));
    let (mode, n) = {
        let mut s = state.lock().unwrap();
        s.requests += 1;
        (s.mode, s.requests)
    };
    match mode {
        Mode::Healthy => reply(&mut stream, "200 OK", "ok"),
        Mode::Error => reply(&mut stream, "500 Internal Server Error", "worker crashed"),
        Mode::Down => drop(stream),
        Mode::Climbing => {
            std::thread::sleep(Duration::from_millis((40 * (n % 50)).min(3000)));
            reply(&mut stream, "200 OK", "ok, slowly");
        }
        Mode::PrimaryOnly if primary => reply(&mut stream, "500 Internal Server Error", "rejecting this client"),
        Mode::PrimaryOnly => reply(&mut stream, "200 OK", "ok"),
    }
}

fn steer(mut stream: TcpStream, state: &Mutex<State>) {
    let head = read_head(&mut stream);
    let first = head.lines().next().unwrap_or("");
    let mut parts = first.split_whitespace();
    let (method, path) = (parts.next().unwrap_or(""), parts.next().unwrap_or(""));
    let mut s = state.lock().unwrap();
    match (method, path) {
        ("POST", "/restart") => {
            s.mode = Mode::Healthy;
            s.requests = 0;
            s.restarts += 1;
            let body = format!("restarted {} time(s)", s.restarts);
            drop(s);
            reply(&mut stream, "200 OK", &body);
        }
        ("POST", p) if p.starts_with("/mode/") => match Mode::parse(&p[6..]) {
            Some(m) => {
                s.mode = m;
                s.requests = 0;
                drop(s);
                reply(&mut stream, "200 OK", m.name());
            }
            None => {
                drop(s);
                reply(&mut stream, "400 Bad Request", "modes: healthy error down climbing primary-only");
            }
        },
        ("GET", "/mode") => {
            let body = format!("{} restarts={}", s.mode.name(), s.restarts);
            drop(s);
            reply(&mut stream, "200 OK", &body);
        }
        _ => {
            drop(s);
            reply(&mut stream, "404 Not Found", "POST /mode/<name>, POST /restart, GET /mode");
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::{Kind, Target};
    use crate::probe;
    use crate::verify::{self, Fault, Observations};

    fn target(addr: SocketAddr) -> Target {
        Target {
            name: "fake".into(),
            kind: Kind::Http,
            url: Some(format!("http://{addr}/")),
            address: None,
            path: None,
            max_age_secs: None,
            expected_status: Some(200),
            latency_budget_ms: 1000,
            depends_on: vec![],
            stateless: true,
            restart_url: None,
            restart_cmd: None,
            rollback: None,
            enabled: true,
        }
    }

    fn set(control: SocketAddr, path: &str) {
        let mut s = TcpStream::connect(control).unwrap();
        write!(s, "POST {path} HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n").unwrap();
        let mut out = String::new();
        let _ = s.read_to_string(&mut out);
        assert!(out.starts_with("HTTP/1.1 200"), "{out}");
    }

    fn observe(t: &Target, canary_addr: SocketAddr) -> Fault {
        let timeout = Duration::from_secs(2);
        let p = probe::primary(t, timeout);
        let s = probe::secondary(t, timeout);
        let c = probe::canary(&canary_addr.to_string(), timeout);
        verify::classify(&Observations {
            target: &t.name,
            primary: &p,
            secondary: &s,
            canary: &c,
            dependencies: &[],
        })
    }

    #[test]
    fn both_probe_paths_agree_on_each_mode() {
        let fake = start(0, 0).unwrap();
        let t = target(fake.service);
        // The control port doubles as a canary: it is up whenever the machine is.
        assert_eq!(observe(&t, fake.control), Fault::None);

        set(fake.control, "/mode/error");
        assert_eq!(observe(&t, fake.control), Fault::Target("fake".into()));

        set(fake.control, "/mode/down");
        assert_eq!(observe(&t, fake.control), Fault::Target("fake".into()));

        set(fake.control, "/restart");
        assert_eq!(observe(&t, fake.control), Fault::None);
    }

    #[test]
    fn a_probe_path_failure_on_a_healthy_service_is_not_an_outage() {
        let fake = start(0, 0).unwrap();
        let t = target(fake.service);
        set(fake.control, "/mode/primary-only");
        let p = probe::primary(&t, Duration::from_secs(2));
        assert!(!p.ok, "primary path should see the injected 500");
        assert_eq!(observe(&t, fake.control).kind(), "probe");
    }
}
