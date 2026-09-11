//! Method + path router with `{param}` segments (std only).
//!
//! TEMPLATE FILE — copied verbatim into every Rust service from
//! `templates/rust/`. Do not edit inside a service; fix here and re-copy.
//!
//! The router maps `(method, path)` to a route *name*; the service's `http.rs`
//! dispatches on that name. This keeps the router free of service types.
//!
//! ```ignore
//! let mut r = Router::new();
//! r.add("GET", "/healthz", "healthz")
//!  .add("POST", "/token", "token_issue")
//!  .add("GET", "/keys/{kid}", "key_get");
//! match r.resolve("GET", "/keys/key-01") {
//!     Resolution::Found { name, params } => { /* name == "key_get", params.get("kid") */ }
//!     Resolution::MethodNotAllowed { allowed } => { /* 405 */ }
//!     Resolution::NotFound => { /* 404 */ }
//! }
//! ```

#[derive(Debug, Clone, Default, PartialEq)]
pub struct Params(Vec<(String, String)>);

impl Params {
    pub fn get(&self, name: &str) -> Option<&str> {
        self.0.iter().find(|(k, _)| k == name).map(|(_, v)| v.as_str())
    }

    pub fn iter(&self) -> impl Iterator<Item = (&str, &str)> {
        self.0.iter().map(|(k, v)| (k.as_str(), v.as_str()))
    }
}

#[derive(Debug, Clone, PartialEq)]
enum Segment {
    Literal(String),
    Param(String),
}

#[derive(Debug, Clone)]
struct Route {
    method: &'static str,
    segments: Vec<Segment>,
    name: &'static str,
}

#[derive(Debug, Default)]
pub struct Router {
    routes: Vec<Route>,
}

#[derive(Debug, PartialEq)]
pub enum Resolution {
    Found { name: &'static str, params: Params },
    MethodNotAllowed { allowed: Vec<&'static str> },
    NotFound,
}

impl Router {
    pub fn new() -> Self {
        Self::default()
    }

    /// Register a route. `pattern` is a path like `/orders/{id}/fills`.
    pub fn add(&mut self, method: &'static str, pattern: &'static str, name: &'static str) -> &mut Self {
        let segments = split_path(pattern)
            .map(|s| {
                if s.len() > 2 && s.starts_with('{') && s.ends_with('}') {
                    Segment::Param(s[1..s.len() - 1].to_string())
                } else {
                    Segment::Literal(s.to_string())
                }
            })
            .collect();
        self.routes.push(Route { method, segments, name });
        self
    }

    /// Resolve a request. `path` must already be stripped of its query string
    /// (see `split_target`). Routes are tried in registration order.
    pub fn resolve(&self, method: &str, path: &str) -> Resolution {
        let parts: Vec<&str> = split_path(path).collect();
        let mut allowed: Vec<&'static str> = Vec::new();
        for route in &self.routes {
            if let Some(params) = match_segments(&route.segments, &parts) {
                if route.method.eq_ignore_ascii_case(method) {
                    return Resolution::Found { name: route.name, params };
                }
                if !allowed.contains(&route.method) {
                    allowed.push(route.method);
                }
            }
        }
        if allowed.is_empty() {
            Resolution::NotFound
        } else {
            Resolution::MethodNotAllowed { allowed }
        }
    }

    /// `(method, pattern, name)` for every registered route — handy for a
    /// `/routes` debug endpoint or docs.
    pub fn routes(&self) -> Vec<(&'static str, String, &'static str)> {
        self.routes
            .iter()
            .map(|r| {
                let pattern = r
                    .segments
                    .iter()
                    .map(|s| match s {
                        Segment::Literal(l) => l.clone(),
                        Segment::Param(p) => format!("{{{}}}", p),
                    })
                    .collect::<Vec<_>>()
                    .join("/");
                (r.method, format!("/{}", pattern), r.name)
            })
            .collect()
    }
}

fn split_path(path: &str) -> impl Iterator<Item = &str> {
    path.split('/').filter(|s| !s.is_empty())
}

fn match_segments(segments: &[Segment], parts: &[&str]) -> Option<Params> {
    if segments.len() != parts.len() {
        return None;
    }
    let mut params = Vec::new();
    for (seg, part) in segments.iter().zip(parts) {
        match seg {
            Segment::Literal(lit) => {
                if lit != part {
                    return None;
                }
            }
            Segment::Param(name) => params.push((name.clone(), percent_decode(part))),
        }
    }
    Some(Params(params))
}

/// Split a request target into `(path, query pairs)`.
pub fn split_target(target: &str) -> (&str, Vec<(String, String)>) {
    match target.find('?') {
        Some(i) => (&target[..i], parse_query(&target[i + 1..])),
        None => (target, Vec::new()),
    }
}

/// Parse `a=1&b=x%20y&flag` into decoded pairs (`flag` → `("flag", "")`).
pub fn parse_query(query: &str) -> Vec<(String, String)> {
    query
        .split('&')
        .filter(|p| !p.is_empty())
        .map(|p| {
            let (k, v) = match p.find('=') {
                Some(i) => (&p[..i], &p[i + 1..]),
                None => (p, ""),
            };
            (percent_decode(k), percent_decode(v))
        })
        .collect()
}

/// Decode `%XX` sequences and `+` as space. Invalid sequences pass through.
pub fn percent_decode(s: &str) -> String {
    let bytes = s.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        match bytes[i] {
            b'%' if i + 2 < bytes.len() => {
                match (hex_val(bytes[i + 1]), hex_val(bytes[i + 2])) {
                    (Some(h), Some(l)) => {
                        out.push(h * 16 + l);
                        i += 3;
                    }
                    _ => {
                        out.push(b'%');
                        i += 1;
                    }
                }
            }
            b'+' => {
                out.push(b' ');
                i += 1;
            }
            c => {
                out.push(c);
                i += 1;
            }
        }
    }
    String::from_utf8_lossy(&out).into_owned()
}

fn hex_val(c: u8) -> Option<u8> {
    match c {
        b'0'..=b'9' => Some(c - b'0'),
        b'a'..=b'f' => Some(c - b'a' + 10),
        b'A'..=b'F' => Some(c - b'A' + 10),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn router() -> Router {
        let mut r = Router::new();
        r.add("GET", "/healthz", "healthz")
            .add("POST", "/token", "token_issue")
            .add("GET", "/keys/{kid}", "key_get")
            .add("DELETE", "/keys/{kid}", "key_delete")
            .add("GET", "/orders/{id}/fills", "fills");
        r
    }

    #[test]
    fn literal_and_param_matching() {
        let r = router();
        assert_eq!(
            r.resolve("GET", "/healthz"),
            Resolution::Found { name: "healthz", params: Params::default() }
        );
        // Trailing slash and method case are tolerated.
        assert!(matches!(r.resolve("get", "/healthz/"), Resolution::Found { name: "healthz", .. }));
        match r.resolve("GET", "/keys/key-0001") {
            Resolution::Found { name, params } => {
                assert_eq!(name, "key_get");
                assert_eq!(params.get("kid"), Some("key-0001"));
            }
            other => panic!("unexpected {:?}", other),
        }
        match r.resolve("GET", "/orders/abc%20def/fills") {
            Resolution::Found { name, params } => {
                assert_eq!(name, "fills");
                assert_eq!(params.get("id"), Some("abc def"));
            }
            other => panic!("unexpected {:?}", other),
        }
    }

    #[test]
    fn not_found_and_method_not_allowed() {
        let r = router();
        assert_eq!(r.resolve("GET", "/nope"), Resolution::NotFound);
        assert_eq!(r.resolve("GET", "/keys"), Resolution::NotFound);
        assert_eq!(
            r.resolve("PUT", "/keys/k1"),
            Resolution::MethodNotAllowed { allowed: vec!["GET", "DELETE"] }
        );
    }

    #[test]
    fn query_parsing() {
        let (path, q) = split_target("/a/b?x=1&y=hello%20world&z=a+b&flag");
        assert_eq!(path, "/a/b");
        assert_eq!(q[0], ("x".to_string(), "1".to_string()));
        assert_eq!(q[1].1, "hello world");
        assert_eq!(q[2].1, "a b");
        assert_eq!(q[3], ("flag".to_string(), String::new()));
        assert_eq!(split_target("/plain").1.len(), 0);
        assert_eq!(percent_decode("100%"), "100%");
    }

    #[test]
    fn routes_listing() {
        let r = router();
        let listed = r.routes();
        assert_eq!(listed[2], ("GET", "/keys/{kid}".to_string(), "key_get"));
    }
}