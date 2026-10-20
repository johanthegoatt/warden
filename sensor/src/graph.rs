use std::collections::{BTreeMap, BTreeSet};

pub type Edges = BTreeMap<String, Vec<String>>;

/// Depth-first search with three colours. Returns the first cycle found as a
/// path that starts and ends on the same node.
pub fn find_cycle(edges: &Edges) -> Option<Vec<String>> {
    #[derive(Clone, Copy, PartialEq)]
    enum Mark {
        Visiting,
        Done,
    }
    fn visit(
        node: &str,
        edges: &Edges,
        marks: &mut BTreeMap<String, Mark>,
        stack: &mut Vec<String>,
    ) -> Option<Vec<String>> {
        match marks.get(node) {
            Some(Mark::Done) => return None,
            Some(Mark::Visiting) => {
                let start = stack.iter().position(|n| n == node).unwrap_or(0);
                let mut cycle = stack[start..].to_vec();
                cycle.push(node.to_string());
                return Some(cycle);
            }
            None => {}
        }
        marks.insert(node.to_string(), Mark::Visiting);
        stack.push(node.to_string());
        for next in edges.get(node).into_iter().flatten() {
            if let Some(c) = visit(next, edges, marks, stack) {
                return Some(c);
            }
        }
        stack.pop();
        marks.insert(node.to_string(), Mark::Done);
        None
    }

    let mut marks = BTreeMap::new();
    for node in edges.keys() {
        let mut stack = Vec::new();
        if let Some(c) = visit(node, edges, &mut marks, &mut stack) {
            return Some(c);
        }
    }
    None
}

/// Everything `node` depends on, directly or transitively, nearest first.
pub fn upstream(edges: &Edges, node: &str) -> Vec<String> {
    let mut seen = BTreeSet::new();
    let mut order = Vec::new();
    let mut queue: std::collections::VecDeque<String> =
        edges.get(node).cloned().unwrap_or_default().into();
    while let Some(n) = queue.pop_front() {
        if n == node || !seen.insert(n.clone()) {
            continue;
        }
        order.push(n.clone());
        for next in edges.get(&n).into_iter().flatten() {
            queue.push_back(next.clone());
        }
    }
    order
}

#[cfg(test)]
mod tests {
    use super::*;

    fn edges(pairs: &[(&str, &[&str])]) -> Edges {
        pairs
            .iter()
            .map(|(k, v)| (k.to_string(), v.iter().map(|s| s.to_string()).collect()))
            .collect()
    }

    #[test]
    fn no_cycle_in_a_chain() {
        let e = edges(&[("bot", &["site"]), ("site", &["edge"]), ("edge", &[])]);
        assert!(find_cycle(&e).is_none());
    }

    #[test]
    fn finds_a_cycle() {
        let e = edges(&[("a", &["b"]), ("b", &["c"]), ("c", &["a"])]);
        let c = find_cycle(&e).unwrap();
        assert_eq!(c.first(), c.last());
        assert_eq!(c.len(), 4);
    }

    #[test]
    fn upstream_is_transitive_and_nearest_first() {
        let e = edges(&[
            ("bot", &["site", "discord"]),
            ("site", &["edge"]),
            ("discord", &[]),
            ("edge", &[]),
        ]);
        assert_eq!(upstream(&e, "bot"), vec!["site", "discord", "edge"]);
        assert!(upstream(&e, "edge").is_empty());
    }
}
