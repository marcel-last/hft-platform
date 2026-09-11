//! execution_gateway — minimal JSON value model + (de)serializer.
//!
//! The platform constraint forbids external crates, so this module provides a
//! small, correct-enough-for-our-wire-format JSON implementation using only
//! `std`.  It supports the subset of JSON we actually exchange: objects (with
//! insertion-ordered keys), arrays, strings, numbers (i64 / f64), booleans and
//! null.  Encoding is canonical-ish (compact); decoding tolerates whitespace.

use std::fmt;

/// A JSON value.  Objects preserve key insertion order.
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

/// An insertion-ordered map of JSON object keys.
pub type Map = Vec<(String, Value)>;

impl Value {
    // -- constructors -------------------------------------------------------

    pub fn obj(pairs: Vec<(String, Value)>) -> Self {
        Value::Object(pairs)
    }

    pub fn arr(items: Vec<Value>) -> Self {
        Value::Array(items)
    }

    // -- accessors ----------------------------------------------------------

    /// Look up an object member by key (returns `None` if not an object/missing).
    pub fn get(&self, key: &str) -> Option<&Value> {
        match self {
            Value::Object(pairs) => pairs.iter().find(|(k, _)| k == key).map(|(_, v)| v),
            _ => None,
        }
    }

    pub fn as_str(&self) -> Option<&str> {
        match self {
            Value::String(s) => Some(s.as_str()),
            _ => None,
        }
    }

    pub fn as_i64(&self) -> Option<i64> {
        match self {
            Value::Int(i) => Some(*i),
            Value::Float(f) if f.fract() == 0.0 && f.abs() < 9.2e18 => Some(*f as i64),
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

    pub fn as_bool(&self) -> Option<bool> {
        match self {
            Value::Bool(b) => Some(*b),
            _ => None,
        }
    }

    pub fn as_array(&self) -> Option<&Vec<Value>> {
        match self {
            Value::Array(a) => Some(a),
            _ => None,
        }
    }

    /// True if the value is a JSON object.
    pub fn is_object(&self) -> bool {
        matches!(self, Value::Object(_))
    }

    // -- encoding -----------------------------------------------------------

    /// Render this value to compact JSON.
    pub fn to_json(&self) -> String {
        let mut out = String::new();
        self.write_to(&mut out);
        out
    }

    fn write_to(&self, out: &mut String) {
        match self {
            Value::Null => out.push_str("null"),
            Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
            Value::Int(i) => out.push_str(&i.to_string()),
            Value::Float(f) => {
                if f.is_finite() {
                    out.push_str(&format_float(*f));
                } else {
                    out.push_str("null")
                }
            }
            Value::String(s) => write_json_string(out, s),
            Value::Array(items) => {
                out.push('[');
                for (i, item) in items.iter().enumerate() {
                    if i > 0 {
                        out.push(',');
                    }
                    item.write_to(out);
                }
                out.push(']');
            }
            Value::Object(pairs) => {
                out.push('{');
                for (i, (k, v)) in pairs.iter().enumerate() {
                    if i > 0 {
                        out.push(',');
                    }
                    write_json_string(out, k);
                    out.push(':');
                    v.write_to(out);
                }
                out.push('}');
            }
        }
    }
}

fn format_float(f: f64) -> String {
    // Emit integers without a trailing ".0" to keep wire output clean.
    if f.fract() == 0.0 && f.abs() < 9.2e18 {
        format!("{}", f as i64)
    } else {
        format!("{f}")
    }
}

fn write_json_string(out: &mut String, s: &str) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
}

impl fmt::Display for Value {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.to_json())
    }
}

// ---------------------------------------------------------------------------
// Decoder
// ---------------------------------------------------------------------------

/// Decode a JSON document from a UTF-8 string.  Returns `Err` with a message on
/// malformed input.
pub fn parse(s: &str) -> Result<Value, String> {
    let bytes = s.as_bytes();
    let mut pos = 0usize;
    skip_ws(bytes, &mut pos);
    let v = parse_value(bytes, &mut pos)?;
    skip_ws(bytes, &mut pos);
    if pos != bytes.len() {
        return Err(format!("trailing characters at byte {pos}"));
    }
    Ok(v)
}

fn skip_ws(b: &[u8], pos: &mut usize) {
    while *pos < b.len() && matches!(b[*pos], b' ' | b'\t' | b'\n' | b'\r') {
        *pos += 1;
    }
}

fn parse_value(b: &[u8], pos: &mut usize) -> Result<Value, String> {
    skip_ws(b, pos);
    if *pos >= b.len() {
        return Err("unexpected end of input".into());
    }
    match b[*pos] {
        b'{' => parse_object(b, pos),
        b'[' => parse_array(b, pos),
        b'"' => Ok(Value::String(parse_string(b, pos)?)),
        b't' | b'f' => parse_bool(b, pos),
        b'n' => {
            expect_lit(b, pos, "null")?;
            Ok(Value::Null)
        }
        _ => parse_number(b, pos),
    }
}

fn expect_lit(b: &[u8], pos: &mut usize, lit: &str) -> Result<(), String> {
    let end = *pos + lit.len();
    if end > b.len() || std::str::from_utf8(&b[*pos..end]).map_err(|_| "bad utf8")? != lit {
        return Err(format!("expected literal {lit:?}"));
    }
    *pos = end;
    Ok(())
}

fn parse_bool(b: &[u8], pos: &mut usize) -> Result<Value, String> {
    if b[*pos] == b't' {
        expect_lit(b, pos, "true")?;
        Ok(Value::Bool(true))
    } else {
        expect_lit(b, pos, "false")?;
        Ok(Value::Bool(false))
    }
}

fn parse_object(b: &[u8], pos: &mut usize) -> Result<Value, String> {
    *pos += 1; // consume '{'
    let mut pairs = Vec::new();
    skip_ws(b, pos);
    if *pos < b.len() && b[*pos] == b'}' {
        *pos += 1;
        return Ok(Value::Object(pairs));
    }
    loop {
        skip_ws(b, pos);
        let key = parse_string(b, pos)?;
        skip_ws(b, pos);
        if *pos >= b.len() || b[*pos] != b':' {
            return Err("expected ':' in object".into());
        }
        *pos += 1;
        let val = parse_value(b, pos)?;
        pairs.push((key, val));
        skip_ws(b, pos);
        if *pos >= b.len() {
            return Err("unterminated object".into());
        }
        match b[*pos] {
            b',' => {
                *pos += 1;
            }
            b'}' => {
                *pos += 1;
                break;
            }
            _ => return Err("expected ',' or '}' in object".into()),
        }
    }
    Ok(Value::Object(pairs))
}

fn parse_array(b: &[u8], pos: &mut usize) -> Result<Value, String> {
    *pos += 1; // consume '['
    let mut items = Vec::new();
    skip_ws(b, pos);
    if *pos < b.len() && b[*pos] == b']' {
        *pos += 1;
        return Ok(Value::Array(items));
    }
    loop {
        let val = parse_value(b, pos)?;
        items.push(val);
        skip_ws(b, pos);
        if *pos >= b.len() {
            return Err("unterminated array".into());
        }
        match b[*pos] {
            b',' => {
                *pos += 1;
            }
            b']' => {
                *pos += 1;
                break;
            }
            _ => return Err("expected ',' or ']' in array".into()),
        }
    }
    Ok(Value::Array(items))
}

fn parse_string(b: &[u8], pos: &mut usize) -> Result<String, String> {
    if *pos >= b.len() || b[*pos] != b'"' {
        return Err("expected string".into());
    }
    *pos += 1;
    let mut out = String::new();
    while *pos < b.len() {
        let c = b[*pos];
        match c {
            b'"' => {
                *pos += 1;
                return Ok(out);
            }
            b'\\' => {
                *pos += 1;
                if *pos >= b.len() {
                    return Err("dangling escape".into());
                }
                match b[*pos] {
                    b'"' => out.push('"'),
                    b'\\' => out.push('\\'),
                    b'/' => out.push('/'),
                    b'n' => out.push('\n'),
                    b't' => out.push('\t'),
                    b'r' => out.push('\r'),
                    b'b' => out.push('\u{08}'),
                    b'f' => out.push('\u{0c}'),
                    b'u' => {
                        let hex = std::str::from_utf8(&b[*pos + 1..*pos + 5])
                            .map_err(|_| "bad \\u escape")?;
                        let code = u32::from_str_radix(hex, 16).map_err(|e| e.to_string())?;
                        out.push(char::from_u32(code).unwrap_or('\u{fffd}'));
                        *pos += 4;
                    }
                    other => return Err(format!("bad escape \\{other}")),
                }
                *pos += 1;
            }
            _ => {
                // consume a UTF-8 character
                let start = *pos;
                let len = utf8_len(c);
                if start + len > b.len() {
                    return Err("truncated utf8".into());
                }
                out.push_str(
                    std::str::from_utf8(&b[start..start + len]).map_err(|_| "bad utf8")?,
                );
                *pos = start + len;
            }
        }
    }
    Err("unterminated string".into())
}

fn utf8_len(first: u8) -> usize {
    if first < 0x80 {
        1
    } else if first & 0xE0 == 0xC0 {
        2
    } else if first & 0xF0 == 0xE0 {
        3
    } else {
        4
    }
}

fn parse_number(b: &[u8], pos: &mut usize) -> Result<Value, String> {
    let start = *pos;
    while *pos < b.len() && matches!(b[*pos], b'0'..=b'9' | b'-' | b'+' | b'.' | b'e' | b'E') {
        *pos += 1;
    }
    let s = std::str::from_utf8(&b[start..*pos]).map_err(|_| "bad number")?;
    if s.contains('.') || s.contains('e') || s.contains('E') {
        s.parse::<f64>()
            .map(Value::Float)
            .map_err(|e| format!("bad float {s}: {e}"))
    } else {
        s.parse::<i64>()
            .map(Value::Int)
            .or_else(|_| s.parse::<f64>().map(Value::Float))
            .map_err(|e| format!("bad number {s}: {e}"))
    }
}
