//! Two independent ways to look at every target.
//!
//! The primary path uses ureq. The secondary path shares nothing with it:
//! its own DNS lookup, its own socket, Windows' TLS stack driven directly, and a
//! hand-written HTTP/1.1 request. If only one path fails, the failure belongs
//! to that path and not to the target.

use crate::config::{Kind, Target};
use serde::Serialize;
use std::io::{Read, Write};
use std::net::{TcpStream, ToSocketAddrs};
use std::time::{Duration, Instant, SystemTime};

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Outcome {
    pub ok: bool,
    pub status: Option<u16>,
    pub latency_ms: Option<f64>,
    pub error: Option<String>,
    /// The probe could not run at all (bad local state, not a remote answer).
    pub probe_error: bool,
}

impl Outcome {
    fn up(status: Option<u16>, started: Instant) -> Self {
        Outcome {
            ok: true,
            status,
            latency_ms: Some(ms(started)),
            error: None,
            probe_error: false,
        }
    }
    fn wrong_status(status: u16, expected: u16, started: Instant) -> Self {
        Outcome {
            ok: false,
            status: Some(status),
            latency_ms: Some(ms(started)),
            error: Some(format!("status {status}, expected {expected}")),
            probe_error: false,
        }
    }
    fn down(error: String, started: Instant) -> Self {
        Outcome {
            ok: false,
            status: None,
            latency_ms: Some(ms(started)),
            error: Some(error),
            probe_error: false,
        }
    }
    fn broken_probe(error: String) -> Self {
        Outcome {
            ok: false,
            status: None,
            latency_ms: None,
            error: Some(error),
            probe_error: true,
        }
    }
}

fn ms(started: Instant) -> f64 {
    (started.elapsed().as_secs_f64() * 1000.0 * 10.0).round() / 10.0
}

pub fn primary(target: &Target, timeout: Duration) -> Outcome {
    match target.kind {
        Kind::Http => primary_http(target, timeout),
        Kind::Tcp => tcp_connect(target.address.as_deref().unwrap_or(""), timeout, 0),
        Kind::Heartbeat => heartbeat_mtime(target),
    }
}

pub fn secondary(target: &Target, timeout: Duration) -> Outcome {
    match target.kind {
        Kind::Http => secondary_http(target, timeout),
        // A fresh resolve and, when DNS offers more than one, a different address.
        Kind::Tcp => tcp_connect(target.address.as_deref().unwrap_or(""), timeout, 1),
        Kind::Heartbeat => heartbeat_read(target),
    }
}

pub fn canary(address: &str, timeout: Duration) -> Outcome {
    tcp_connect(address, timeout, 0)
}

fn primary_http(target: &Target, timeout: Duration) -> Outcome {
    let url = target.url.as_deref().unwrap_or("");
    let expected = target.expected_status.unwrap_or(200);
    let agent: ureq::Agent = ureq::Agent::config_builder()
        .tls_config(
            ureq::tls::TlsConfig::builder()
                .provider(ureq::tls::TlsProvider::NativeTls)
                .root_certs(ureq::tls::RootCerts::PlatformVerifier)
                .build(),
        )
        .timeout_global(Some(timeout))
        .http_status_as_error(false)
        .max_redirects(0)
        .user_agent("warden-primary/0.1")
        .build()
        .into();
    let started = Instant::now();
    match agent.get(url).call() {
        Ok(resp) => {
            let status = resp.status().as_u16();
            if status == expected {
                Outcome::up(Some(status), started)
            } else {
                Outcome::wrong_status(status, expected, started)
            }
        }
        Err(ureq::Error::BadUri(e)) => Outcome::broken_probe(format!("bad url: {e}")),
        Err(ureq::Error::Tls(e)) => Outcome::down(format!("tls: {e}"), started),
        Err(e) => Outcome::down(e.to_string(), started),
    }
}

pub struct ParsedUrl {
    pub tls: bool,
    pub host: String,
    pub port: u16,
    pub path: String,
}

pub fn parse_url(url: &str) -> Option<ParsedUrl> {
    let (tls, rest) = if let Some(r) = url.strip_prefix("https://") {
        (true, r)
    } else if let Some(r) = url.strip_prefix("http://") {
        (false, r)
    } else {
        return None;
    };
    let (authority, path) = match rest.find('/') {
        Some(i) => (&rest[..i], &rest[i..]),
        None => (rest, "/"),
    };
    let (host, port) = match authority.rsplit_once(':') {
        Some((h, p)) if !p.is_empty() && p.bytes().all(|b| b.is_ascii_digit()) => {
            (h, p.parse().ok()?)
        }
        _ => (authority, if tls { 443 } else { 80 }),
    };
    if host.is_empty() {
        return None;
    }
    Some(ParsedUrl {
        tls,
        host: host.to_string(),
        port,
        path: path.to_string(),
    })
}

fn secondary_http(target: &Target, timeout: Duration) -> Outcome {
    let url = target.url.as_deref().unwrap_or("");
    let expected = target.expected_status.unwrap_or(200);
    let Some(u) = parse_url(url) else {
        return Outcome::broken_probe(format!("cannot parse url {url:?}"));
    };
    let started = Instant::now();
    let addr = match (u.host.as_str(), u.port).to_socket_addrs() {
        Ok(mut a) => match a.next() {
            Some(a) => a,
            None => return Outcome::down(format!("dns: no address for {}", u.host), started),
        },
        Err(e) => return Outcome::down(format!("dns: {e}"), started),
    };
    let stream = match TcpStream::connect_timeout(&addr, timeout) {
        Ok(s) => s,
        Err(e) => return Outcome::down(format!("connect {addr}: {e}"), started),
    };
    let _ = stream.set_read_timeout(Some(timeout));
    let _ = stream.set_write_timeout(Some(timeout));
    let request = format!(
        "GET {} HTTP/1.1\r\nHost: {}\r\nUser-Agent: warden-verify/0.1\r\nAccept: */*\r\nConnection: close\r\n\r\n",
        u.path, u.host
    );
    let head = if u.tls {
        let connector = match native_tls::TlsConnector::new() {
            Ok(c) => c,
            Err(e) => return Outcome::broken_probe(format!("tls setup on this machine: {e}")),
        };
        match connector.connect(&u.host, stream) {
            Ok(mut tls) => exchange(&mut tls, &request),
            Err(e) => return Outcome::down(format!("tls handshake: {e}"), started),
        }
    } else {
        let mut s = stream;
        exchange(&mut s, &request)
    };
    match head {
        Ok(status) if status == expected => Outcome::up(Some(status), started),
        Ok(status) => Outcome::wrong_status(status, expected, started),
        Err(e) => Outcome::down(e, started),
    }
}

/// Sends the request and returns the status code from the first line.
fn exchange<S: Read + Write>(stream: &mut S, request: &str) -> Result<u16, String> {
    stream
        .write_all(request.as_bytes())
        .map_err(|e| format!("write: {e}"))?;
    let mut buf = Vec::with_capacity(256);
    let mut chunk = [0u8; 256];
    while !buf.windows(2).any(|w| w == b"\r\n") {
        let n = stream.read(&mut chunk).map_err(|e| format!("read: {e}"))?;
        if n == 0 {
            return Err("connection closed before a status line".into());
        }
        buf.extend_from_slice(&chunk[..n]);
        if buf.len() > 8192 {
            return Err("status line longer than 8 KB".into());
        }
    }
    let line = String::from_utf8_lossy(&buf);
    let first = line.lines().next().unwrap_or("");
    let mut parts = first.split_whitespace();
    match (parts.next(), parts.next().and_then(|c| c.parse::<u16>().ok())) {
        (Some(v), Some(code)) if v.starts_with("HTTP/") => Ok(code),
        _ => Err(format!("not an HTTP status line: {first:?}")),
    }
}

fn tcp_connect(address: &str, timeout: Duration, prefer_index: usize) -> Outcome {
    let started = Instant::now();
    let addrs: Vec<_> = match address.to_socket_addrs() {
        Ok(a) => a.collect(),
        Err(e) => return Outcome::down(format!("dns {address}: {e}"), started),
    };
    let Some(addr) = addrs.get(prefer_index).or(addrs.first()) else {
        return Outcome::down(format!("dns: no address for {address}"), started);
    };
    match TcpStream::connect_timeout(addr, timeout) {
        Ok(_) => Outcome::up(None, started),
        Err(e) => Outcome::down(format!("connect {addr}: {e}"), started),
    }
}

fn heartbeat_mtime(target: &Target) -> Outcome {
    let started = Instant::now();
    let path = target.path.as_deref().unwrap_or("");
    let max_age = target.max_age_secs.unwrap_or(300);
    match std::fs::metadata(path).and_then(|m| m.modified()) {
        Ok(modified) => judge_age(modified, max_age, started),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            Outcome::down(format!("{path} does not exist; the process never wrote it"), started)
        }
        Err(e) => Outcome::broken_probe(format!("cannot stat {path}: {e}")),
    }
}

fn heartbeat_read(target: &Target) -> Outcome {
    let started = Instant::now();
    let path = target.path.as_deref().unwrap_or("");
    let max_age = target.max_age_secs.unwrap_or(300);
    match std::fs::File::open(path) {
        Ok(f) => match f.metadata().and_then(|m| m.modified()) {
            Ok(modified) => judge_age(modified, max_age, started),
            Err(e) => Outcome::broken_probe(format!("opened {path} but cannot read its time: {e}")),
        },
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            Outcome::down(format!("{path} does not exist; the process never wrote it"), started)
        }
        Err(e) => Outcome::broken_probe(format!("cannot open {path}: {e}")),
    }
}

fn judge_age(modified: SystemTime, max_age: u64, started: Instant) -> Outcome {
    let age = SystemTime::now()
        .duration_since(modified)
        .unwrap_or_default()
        .as_secs();
    if age <= max_age {
        Outcome::up(None, started)
    } else {
        Outcome::down(
            format!("last heartbeat {age}s ago, limit is {max_age}s"),
            started,
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_urls() {
        let u = parse_url("https://icarus.johanthegoat.xyz/api/interactions").unwrap();
        assert!(u.tls);
        assert_eq!(u.host, "icarus.johanthegoat.xyz");
        assert_eq!(u.port, 443);
        assert_eq!(u.path, "/api/interactions");
        let u = parse_url("http://127.0.0.1:8099").unwrap();
        assert!(!u.tls);
        assert_eq!(u.port, 8099);
        assert_eq!(u.path, "/");
        assert!(parse_url("ftp://x").is_none());
    }

    #[test]
    fn closed_port_is_down_not_a_probe_error() {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        drop(listener);
        let o = tcp_connect(&addr.to_string(), Duration::from_millis(500), 0);
        assert!(!o.ok);
        assert!(!o.probe_error);
    }
}
