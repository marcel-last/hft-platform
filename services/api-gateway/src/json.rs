//! Hand-rolled JSON codec (std only).
//!
//! TEMPLATE FILE — copied verbatim into every Rust service from
//! `templates/rust/`. Do not edit inside a service; fix here and re-copy.
//!
//! `Value::Object` is an ordered `Vec<(String, Value)>` so serialization is
//! deterministic and key order is preserved. Integers are `i64`; any number
//! with a fraction or exponent is `f64`. Floats always serialize with a
//! decimal point so they round-trip as `Float`, not `Int`.

use std::fmt;

#[derive(Debug, Clone, PartialEq)]
pub enum Value {
    Null,
    Bool(bool),
    Int(i64),
    Float(f64),
    String(String),
    Array(Vec<Value>),
    Object(Vec<(String, Value)>),
}

#[derive(Debug, Clone, PartialEq)]
pub struct JsonError {
    pub message: String,
    pub offset: usize,
}

impl fmt::Display for JsonError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{} at byte {}", self.message, self.offset)
    }
}

impl std::error::Error for JsonError {}

impl Value {
    /// Convenience constructor: `Value::object(vec![("a", 1.into())])`.
    pub fn object(pairs: Vec<(&str, Value)>) -> Value {
        Value::Object(pairs.into_iter().map(|(k, v)| (k.to_string(), v)).collect())
    }

    pub fn get(&self, key: &str) -> Option<&Value> {
        match self {
            Value::Object(pairs) => pairs.iter().find(|(k, _)| k == key).map(|(_, v)| v),
            _ => None,
        }
    }

    /// Insert or replace a key on an object. No-op on non-objects.
    pub fn set(&mut self, key: &str, value: Value) {
        if let Value::Object(pairs) = self {
            match pairs.iter_mut().find(|(k, _)| k == key) {
                Some(slot) => slot.1 = value,
                None => pairs.push((key.to_string(), value)),
            }
        }
    }

    pub fn as_i64(&self) -> Option<i64> {
        match self {
            Value::Int(i) => Some(*i),
            Value::Float(f) if f.fract() == 0.0 && f.abs() < 9.007_199_254_740_992e15 => {
                Some(*f as i64)
            }
            _ => None,
        }
    }

    pub fn as_f64(&self) -> Option<f64> {
        match self {
            Value::Int(i) => Some(*i as f64),
            Value::Float(f) => Some(*f),
            _ => None,
        }
    }

    pub fn as_str(&self) -> Option<&str> {
        match self {
            Value::String(s) => Some(s.as_str()),
            _ => None,
        }
    }

    pub fn as_bool(&self) -> Option<bool> {
        match self {
            Value::Bool(b) => Some(*b),
            _ => None,
        }
    }

    pub fn as_array(&self) -> Option<&[Value]> {
        match self {
            Value::Array(items) => Some(items.as_slice()),
            _ => None,
        }
    }

    pub fn as_object(&self) -> Option<&[(String, Value)]> {
        match self {
            Value::Object(pairs) => Some(pairs.as_slice()),
            _ => None,
        }
    }

    pub fn is_null(&self) -> bool {
        matches!(self, Value::Null)
    }

    pub fn to_json(&self) -> String {
        let mut out = String::new();
        write_value(self, &mut out);
        out
    }
}

impl fmt::Display for Value {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.to_json())
    }
}

impl From<bool> for Value {
    fn from(b: bool) -> Self {
        Value::Bool(b)
    }
}
impl From<i64> for Value {
    fn from(i: i64) -> Self {
        Value::Int(i)
    }
}
impl From<i32> for Value {
    fn from(i: i32) -> Self {
        Value::Int(i as i64)
    }
}
impl From<u64> for Value {
    /// Saturates at `i64::MAX`; counters never realistically get there.
    fn from(u: u64) -> Self {
        Value::Int(i64::try_from(u).unwrap_or(i64::MAX))
    }
}
impl From<usize> for Value {
    fn from(u: usize) -> Self {
        Value::Int(i64::try_from(u).unwrap_or(i64::MAX))
    }
}
impl From<f64> for Value {
    fn from(f: f64) -> Self {
        Value::Float(f)
    }
}
impl From<&str> for Value {
    fn from(s: &str) -> Self {
        Value::String(s.to_string())
    }
}
impl From<String> for Value {
    fn from(s: String) -> Self {
        Value::String(s)
    }
}
impl From<Vec<Value>> for Value {
    fn from(v: Vec<Value>) -> Self {
        Value::Array(v)
    }
}
impl<T: Into<Value>> From<Option<T>> for Value {
    fn from(o: Option<T>) -> Self {
        match o {
            Some(v) => v.into(),
            None => Value::Null,
        }
    }
}

// ---------------------------------------------------------------------------
// Serializer
// ---------------------------------------------------------------------------

fn write_value(v: &Value, out: &mut String) {
    match v {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Int(i) => out.push_str(&i.to_string()),
        Value::Float(f) => write_float(*f, out),
        Value::String(s) => write_string(s, out),
        Value::Array(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_value(item, out);
            }
            out.push(']');
        }
        Value::Object(pairs) => {
            out.push('{');
            for (i, (k, v)) in pairs.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_string(k, out);
                out.push(':');
                write_value(v, out);
            }
            out.push('}');
        }
    }
}

fn write_float(f: f64, out: &mut String) {
    if !f.is_finite() {
        out.push_str("null");
        return;
    }
    let s = format!("{}", f);
    out.push_str(&s);
    if !s.contains(['.', 'e', 'E']) {
        out.push_str(".0");
    }
}

fn write_string(s: &str, out: &mut String) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
}

// ---------------------------------------------------------------------------
// Parser
// ---------------------------------------------------------------------------

const MAX_DEPTH: usize = 128;

pub fn parse(input: &str) -> Result<Value, JsonError> {
    let mut p = Parser { bytes: input.as_bytes(), pos: 0, depth: 0 };
    p.skip_ws();
    let v = p.value()?;
    p.skip_ws();
    if p.pos != p.bytes.len() {
        return Err(p.err("trailing characters after JSON value"));
    }
    Ok(v)
}

struct Parser<'a> {
    bytes: &'a [u8],
    pos: usize,
    depth: usize,
}

impl<'a> Parser<'a> {
    fn err(&self, message: &str) -> JsonError {
        JsonError { message: message.to_string(), offset: self.pos }
    }

    fn peek(&self) -> Option<u8> {
        self.bytes.get(self.pos).copied()
    }

    fn skip_ws(&mut self) {
        while matches!(self.peek(), Some(b' ' | b'\t' | b'\n' | b'\r')) {
            self.pos += 1;
        }
    }

    fn expect(&mut self, b: u8) -> Result<(), JsonError> {
        if self.peek() == Some(b) {
            self.pos += 1;
            Ok(())
        } else {
            Err(self.err(&format!("expected '{}'", b as char)))
        }
    }

    fn enter(&mut self) -> Result<(), JsonError> {
        self.depth += 1;
        if self.depth > MAX_DEPTH {
            return Err(self.err("nesting too deep"));
        }
        Ok(())
    }

    fn value(&mut self) -> Result<Value, JsonError> {
        match self.peek() {
            None => Err(self.err("unexpected end of input")),
            Some(b'{') => self.object(),
            Some(b'[') => self.array(),
            Some(b'"') => self.string().map(Value::String),
            Some(b't') => self.literal("true", Value::Bool(true)),
            Some(b'f') => self.literal("false", Value::Bool(false)),
            Some(b'n') => self.literal("null", Value::Null),
            Some(c) if c == b'-' || c.is_ascii_digit() => self.number(),
            Some(_) => Err(self.err("unexpected character")),
        }
    }

    fn object(&mut self) -> Result<Value, JsonError> {
        self.enter()?;
        self.pos += 1; // '{'
        let mut pairs = Vec::new();
        self.skip_ws();
        if self.peek() == Some(b'}') {
            self.pos += 1;
            self.depth -= 1;
            return Ok(Value::Object(pairs));
        }
        loop {
            self.skip_ws();
            if self.peek() != Some(b'"') {
                return Err(self.err("expected string key"));
            }
            let key = self.string()?;
            self.skip_ws();
            self.expect(b':')?;
            self.skip_ws();
            let val = self.value()?;
            pairs.push((key, val));
            self.skip_ws();
            match self.peek() {
                Some(b',') => self.pos += 1,
                Some(b'}') => {
                    self.pos += 1;
                    break;
                }
                _ => return Err(self.err("expected ',' or '}'")),
            }
        }
        self.depth -= 1;
        Ok(Value::Object(pairs))
    }

    fn array(&mut self) -> Result<Value, JsonError> {
        self.enter()?;
        self.pos += 1; // '['
        let mut items = Vec::new();
        self.skip_ws();
        if self.peek() == Some(b']') {
            self.pos += 1;
            self.depth -= 1;
            return Ok(Value::Array(items));
        }
        loop {
            self.skip_ws();
            items.push(self.value()?);
            self.skip_ws();
            match self.peek() {
                Some(b',') => self.pos += 1,
                Some(b']') => {
                    self.pos += 1;
                    break;
                }
                _ => return Err(self.err("expected ',' or ']'")),
            }
        }
        self.depth -= 1;
        Ok(Value::Array(items))
    }

    fn literal(&mut self, word: &str, v: Value) -> Result<Value, JsonError> {
        if self.bytes[self.pos..].starts_with(word.as_bytes()) {
            self.pos += word.len();
            Ok(v)
        } else {
            Err(self.err("invalid literal"))
        }
    }

    fn digits(&mut self) -> usize {
        let start = self.pos;
        while matches!(self.peek(), Some(c) if c.is_ascii_digit()) {
            self.pos += 1;
        }
        self.pos - start
    }

    fn number(&mut self) -> Result<Value, JsonError> {
        let start = self.pos;
        if self.peek() == Some(b'-') {
            self.pos += 1;
        }
        match self.peek() {
            Some(b'0') => self.pos += 1,
            Some(c) if c.is_ascii_digit() => {
                self.digits();
            }
            _ => return Err(self.err("invalid number")),
        }
        let mut is_float = false;
        if self.peek() == Some(b'.') {
            is_float = true;
            self.pos += 1;
            if self.digits() == 0 {
                return Err(self.err("expected digits after '.'"));
            }
        }
        if matches!(self.peek(), Some(b'e' | b'E')) {
            is_float = true;
            self.pos += 1;
            if matches!(self.peek(), Some(b'+' | b'-')) {
                self.pos += 1;
            }
            if self.digits() == 0 {
                return Err(self.err("expected digits in exponent"));
            }
        }
        // The slice is pure ASCII by construction.
        let text = std::str::from_utf8(&self.bytes[start..self.pos]).unwrap_or("");
        if !is_float {
            if let Ok(i) = text.parse::<i64>() {
                return Ok(Value::Int(i));
            }
        }
        text.parse::<f64>()
            .map(Value::Float)
            .map_err(|_| self.err("invalid number"))
    }

    fn hex4(&mut self) -> Result<u32, JsonError> {
        let end = self.pos + 4;
        if end > self.bytes.len() {
            return Err(self.err("truncated \\u escape"));
        }
        let text = std::str::from_utf8(&self.bytes[self.pos..end]).unwrap_or("");
        let v = u32::from_str_radix(text, 16).map_err(|_| self.err("invalid \\u escape"))?;
        self.pos = end;
        Ok(v)
    }

    fn string(&mut self) -> Result<String, JsonError> {
        self.pos += 1; // opening quote
        let mut out = String::new();
        loop {
            let c = match self.peek() {
                None => return Err(self.err("unterminated string")),
                Some(c) => c,
            };
            match c {
                b'"' => {
                    self.pos += 1;
                    return Ok(out);
                }
                b'\\' => {
                    self.pos += 1;
                    let e = match self.peek() {
                        None => return Err(self.err("unterminated escape")),
                        Some(e) => e,
                    };
                    self.pos += 1;
                    match e {
                        b'"' => out.push('"'),
                        b'\\' => out.push('\\'),
                        b'/' => out.push('/'),
                        b'b' => out.push('\u{8}'),
                        b'f' => out.push('\u{c}'),
                        b'n' => out.push('\n'),
                        b'r' => out.push('\r'),
                        b't' => out.push('\t'),
                        b'u' => {
                            let mut cp = self.hex4()?;
                            if (0xD800..=0xDBFF).contains(&cp) {
                                if !self.bytes[self.pos..].starts_with(b"\\u") {
                                    return Err(self.err("missing low surrogate"));
                                }
                                self.pos += 2;
                                let lo = self.hex4()?;
                                if !(0xDC00..=0xDFFF).contains(&lo) {
                                    return Err(self.err("invalid low surrogate"));
                                }
                                cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
                            }
                            match char::from_u32(cp) {
                                Some(ch) => out.push(ch),
                                None => return Err(self.err("invalid unicode escape")),
                            }
                        }
                        _ => return Err(self.err("invalid escape")),
                    }
                }
                c if c < 0x20 => return Err(self.err("control character in string")),
                _ => {
                    // Copy a run of raw bytes; run boundaries are ASCII so the
                    // slice is always on a char boundary.
                    let start = self.pos;
                    while let Some(c) = self.peek() {
                        if c == b'"' || c == b'\\' || c < 0x20 {
                            break;
                        }
                        self.pos += 1;
                    }
                    let run = std::str::from_utf8(&self.bytes[start..self.pos])
                        .map_err(|_| self.err("invalid UTF-8"))?;
                    out.push_str(run);
                }
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Tests (run under `cargo test` in every service that copies this file)
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn roundtrip_object() {
        let v = Value::object(vec![
            ("a", 1i64.into()),
            ("b", "x \"y\"\n".into()),
            ("c", true.into()),
            ("d", 2.5f64.into()),
            ("e", Value::Array(vec![1i64.into(), Value::Null])),
            ("f", Value::object(vec![])),
        ]);
        let s = v.to_json();
        assert_eq!(parse(&s).unwrap(), v);
    }

    #[test]
    fn floats_keep_decimal_point() {
        assert_eq!(Value::Float(1.0).to_json(), "1.0");
        assert_eq!(parse("1.0").unwrap(), Value::Float(1.0));
        assert_eq!(parse("1").unwrap(), Value::Int(1));
        assert_eq!(parse("-0").unwrap(), Value::Int(0));
        assert_eq!(parse("1e3").unwrap(), Value::Float(1000.0));
        assert_eq!(parse("9223372036854775807").unwrap(), Value::Int(i64::MAX));
    }

    #[test]
    fn unicode_escapes() {
        assert_eq!(parse(r#""\u00e9\ud83d\ude00""#).unwrap().as_str(), Some("é😀"));
        assert_eq!(parse(r#""caf\u00e9""#).unwrap().as_str(), Some("café"));
        assert_eq!(Value::from("\u{1}").to_json(), r#""\u0001""#);
    }

    #[test]
    fn rejects_garbage() {
        assert!(parse("{} extra").is_err());
        assert!(parse("{bad}").is_err());
        assert!(parse("[1,]").is_err());
        assert!(parse("01").is_err());
        assert!(parse("\"unterminated").is_err());
        assert!(parse("").is_err());
        let deep = "[".repeat(200) + &"]".repeat(200);
        assert!(parse(&deep).is_err());
    }

    #[test]
    fn accessors_and_set() {
        let mut v = Value::object(vec![("n", 3u64.into())]);
        assert_eq!(v.get("n").and_then(Value::as_i64), Some(3));
        assert_eq!(v.get("n").and_then(Value::as_f64), Some(3.0));
        assert_eq!(v.get("missing"), None);
        v.set("n", 4i64.into());
        v.set("s", "hi".into());
        assert_eq!(v.to_json(), r#"{"n":4,"s":"hi"}"#);
        assert_eq!(Value::from(u64::MAX).as_i64(), Some(i64::MAX));
    }
}