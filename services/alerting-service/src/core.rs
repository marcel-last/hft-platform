//! alerting_service — alert aggregation core.
//!
//! The :struct:`AlertManager` is the single source of truth for every logical
//! alert in the platform.  It sits behind a `Mutex` and provides:
//!
//! * **Deduplication** — repeated occurrences of the same `(source, category,
//!   context)` within the dedup window collapse into one logical alert whose
//!   occurrence counter is incremented (no new dispatch).
//! * **Suppression** — while a key's most recent alert is still open
//!   (un-acknowledged), further occurrences keep folding into it for up to the
//!   suppression window.
//! * **Escalation** — an un-acknowledged WARNING that ages past
//!   `warning_escalate_after_ns` becomes CRITICAL; an un-acknowledged CRITICAL
//!   that ages past `critical_escalate_after_ns` is flagged `needs_oncall`.
//!   Escalation is evaluated lazily on every read and on each new occurrence.
//! * **Acknowledgement** — a human/operator acknowledges an alert by id; it
//!   stops escalating and moves to the terminal `ACKNOWLEDGED` state.
//!
//! All timestamps are `i64` nanoseconds; all comparisons are integer.

use std::collections::{HashMap, VecDeque};
use std::sync::{Arc, Mutex};

use crate::config::Config;
use crate::errors::Error;
use crate::models::{AckTicket, AlertEvent, AlertState, Severity};

/// The outcome of submitting an alert occurrence.
#[derive(Debug, Clone)]
pub struct SubmitOutcome {
    /// The logical alert id that now represents this occurrence.
    pub alert_id: String,
    /// True when this occurrence was the first (a new logical alert was created).
    pub is_new: bool,
    /// True when this occurrence was folded into an existing open alert.
    pub suppressed: bool,
    /// The current total number of occurrences for this logical alert.
    pub occurrences: u32,
    /// The effective (post-escalation) severity after processing.
    pub severity: Severity,
}

impl SubmitOutcome {
    pub fn to_json(&self) -> crate::json::Value {
        crate::json::Value::Object(vec![
            ("alert_id".into(), crate::json::Value::String(self.alert_id.clone())),
            ("is_new".into(), crate::json::Value::Bool(self.is_new)),
            ("suppressed".into(), crate::json::Value::Bool(self.suppressed)),
            (
                "occurrences".into(),
                crate::json::Value::Int(self.occurrences as i64),
            ),
            (
                "severity".into(),
                crate::json::Value::String(self.severity.as_str().to_string()),
            ),
        ])
    }
}

/// A single logical alert tracked by the manager.
#[derive(Debug, Clone)]
pub struct LogicalAlert {
    pub id: String,
    pub source: String,
    pub category: String,
    /// The dedup key (source|category|context-json).
    pub key: String,
    /// The severity as first observed.
    pub base_severity: Severity,
    /// The effective severity after escalation (>= base_severity).
    pub current_severity: Severity,
    pub message: String,
    pub context: crate::json::Value,
    /// When the logical alert was first created (ns).
    pub created_ns: i64,
    /// When it was last observed (occurrence) — used for escalation aging.
    pub last_seen_ns: i64,
    /// Total occurrences folded into this logical alert (>= 1), saturated at
    /// `history.max_occurrences`.
    pub occurrences: u32,
    pub state: AlertState,
    /// When the alert was acknowledged (ns); `None` while active.
    pub ack_ns: Option<i64>,
    /// Set when an un-acknowledged CRITICAL aged past the on-call threshold.
    pub needs_oncall: bool,
}

impl LogicalAlert {
    pub fn to_json(&self) -> crate::json::Value {
        let mut pairs = vec![
            ("id".into(), crate::json::Value::String(self.id.clone())),
            ("source".into(), crate::json::Value::String(self.source.clone())),
            ("category".into(), crate::json::Value::String(self.category.clone())),
            (
                "severity".into(),
                crate::json::Value::String(self.base_severity.as_str().to_string()),
            ),
            (
                "effective_severity".into(),
                crate::json::Value::String(self.current_severity.as_str().to_string()),
            ),
            ("message".into(), crate::json::Value::String(self.message.clone())),
            ("context".into(), self.context.clone()),
            ("created_ns".into(), crate::json::Value::Int(self.created_ns)),
            (
                "last_seen_ns".into(),
                crate::json::Value::Int(self.last_seen_ns),
            ),
            (
                "occurrences".into(),
                crate::json::Value::Int(self.occurrences as i64),
            ),
            (
                "state".into(),
                crate::json::Value::String(self.state.as_str().to_string()),
            ),
            ("needs_oncall".into(), crate::json::Value::Bool(self.needs_oncall)),
        ];
        if let Some(ack) = self.ack_ns {
            pairs.push(("ack_ns".into(), crate::json::Value::Int(ack)));
        }
        crate::json::Value::Object(pairs)
    }
}

/// A clock source so tests can inject deterministic time.  Production uses
/// `SystemClock`; tests use a fixed or advancing `ManualClock` to exercise the
/// escalation and suppression windows without sleeping on the wall clock.
pub trait Clock: Send + Sync {
    fn now(&self) -> i64;
}

/// The default clock: real system time in nanoseconds since the Unix epoch.
pub struct SystemClock;
impl Clock for SystemClock {
    fn now(&self) -> i64 {
        crate::models::now_ns()
    }
}

/// A test clock whose value is held behind an `AtomicI64` and advanced by a
/// caller-controlled step on each read (or set explicitly).
pub struct ManualClock {
    now: std::sync::atomic::AtomicI64,
    /// Amount to add to the returned time on every call (0 = fixed clock).
    pub step_ns: i64,
}

impl ManualClock {
    pub fn new(start_ns: i64) -> Self {
        ManualClock {
            now: std::sync::atomic::AtomicI64::new(start_ns),
            step_ns: 0,
        }
    }

    /// Set the clock to an absolute value (resets the running value).
    pub fn set(&self, ns: i64) {
        self.now.store(ns, std::sync::atomic::Ordering::SeqCst);
    }
}

impl Clock for ManualClock {
    fn now(&self) -> i64 {
        let cur = self
            .now
            .fetch_add(self.step_ns, std::sync::atomic::Ordering::SeqCst);
        cur + self.step_ns
    }
}

// A shared clock is also a clock (used by tests to share a ManualClock between
// the manager and the test body).
impl<T: Clock + ?Sized> Clock for std::sync::Arc<T> {
    fn now(&self) -> i64 {
        (**self).now()
    }
}

/// Thread-safe store of logical alerts + dispatch log + stats.
pub struct AlertManager {
    cfg: Config,
    clock: Box<dyn Clock>,
    /// All logical alerts keyed by their dedup key (insertion order preserved
    /// via the `alerts` Vec; the map is a fast lookup index).
    by_key: Mutex<HashMap<String, usize>>,
    alerts: Mutex<Vec<LogicalAlert>>,
    /// Bounded log of dispatch events (new + escalated), newest at the back.
    dispatch_log: Mutex<VecDeque<crate::json::Value>>,
    /// Monotonic counters for /stats.
    stats: Mutex<Stats>,
    /// Monotonic alert sequence number for id generation.
    next_seq: Mutex<u64>,
}

/// Aggregate counters exposed by `/stats`.
#[derive(Debug, Default, Clone)]
pub struct Stats {
    /// Total occurrences ever submitted (including suppressed ones).
    pub received_total: u64,
    /// New logical alerts created.
    pub new_alerts_total: u64,
    /// Occurrences folded into an existing open alert.
    pub suppressed_total: u64,
    /// Severity escalations performed (WARNING -> CRITICAL).
    pub escalated_total: u64,
    /// Alerts flagged for on-call.
    pub oncall_total: u64,
    /// Acknowledgements processed.
    pub acks_total: u64,
}

impl AlertManager {
    pub fn new(cfg: Config) -> Self {
        Self::with_clock(cfg, Box::new(SystemClock))
    }

    /// Construct with an explicit clock (used by tests for deterministic time).
    pub fn with_clock(cfg: Config, clock: Box<dyn Clock>) -> Self {
        let cap = cfg.history.retained;
        AlertManager {
            cfg,
            clock,
            by_key: Mutex::new(HashMap::new()),
            alerts: Mutex::new(Vec::new()),
            dispatch_log: Mutex::new(VecDeque::with_capacity(cap)),
            stats: Mutex::new(Stats::default()),
            next_seq: Mutex::new(1),
        }
    }

    // -- mutation -----------------------------------------------------------

    /// Submit an alert occurrence.  Handles dedup/suppression, escalation and
    /// dispatch logging in one pass.  Returns the outcome describing how the
    /// occurrence was treated.
    pub fn submit(&self, event: &AlertEvent) -> Result<SubmitOutcome, Error> {
        // Source allow-list gate (open mode when the list is empty).
        if !self.cfg.allows_source(&event.source) {
            return Err(Error::unauthorized_source(&event.source));
        }

        let key = event.dedup_key();
        let now = self.clock.now();

        // Snapshot of any existing open alert for this key.
        let existing_idx: Option<usize> = {
            let by_key = self.by_key.lock().unwrap();
            by_key.get(&key).copied()
        };

        match existing_idx {
            Some(idx) => {
                let (outcome, dispatch) = {
                    let mut alerts = self.alerts.lock().unwrap();
                    let a = &mut alerts[idx];

                    // Fold only while the alert is still OPEN (un-acknowledged,
                    // un-resolved) AND recent.  An acknowledged/resolved alert, or
                    // one whose suppression window has lapsed, is closed and a
                    // fresh logical alert is opened for the new occurrence.
                    let within_suppression = now.saturating_sub(a.last_seen_ns)
                        <= self.cfg.dedup.suppression_window_ns;
                    if !a.state.is_terminal() && within_suppression {
                        // Fold: increment occurrences, bump severity if higher,
                        // refresh last_seen, re-evaluate escalation.
                        a.occurrences = (a.occurrences + 1).min(self.cfg.history.max_occurrences);
                        a.last_seen_ns = now;
                        if event.severity > a.current_severity {
                            a.current_severity = event.severity;
                        }
                        let before = a.current_severity;
                        self.escalate(a, now);
                        let outcome = SubmitOutcome {
                            alert_id: a.id.clone(),
                            is_new: false,
                            suppressed: true,
                            occurrences: a.occurrences,
                            severity: a.current_severity,
                        };
                        // Log a dispatch only if the effective severity rose.
                        let dispatch = if a.current_severity > before {
                            Some(self.dispatch_entry(a, now))
                        } else {
                            None
                        };
                        (outcome, dispatch)
                    } else {
                        // Stale or terminal: close the old alert and create a new one.
                        a.state = AlertState::Resolved;
                        let seq = self.next_seq();
                        let id = format!("ALT-{seq:08}");
                        let mut na = LogicalAlert {
                            id,
                            source: event.source.clone(),
                            category: event.category.clone(),
                            key: key.clone(),
                            base_severity: event.severity,
                            current_severity: event.severity,
                            message: event.message.clone(),
                            context: event.context.clone(),
                            created_ns: now,
                            last_seen_ns: now,
                            occurrences: 1,
                            state: AlertState::Active,
                            ack_ns: None,
                            needs_oncall: false,
                        };
                        self.escalate(&mut na, now);
                        let outcome = SubmitOutcome {
                            alert_id: na.id.clone(),
                            is_new: true,
                            suppressed: false,
                            occurrences: 1,
                            severity: na.current_severity,
                        };
                        let dispatch = Some(self.dispatch_entry(&na, now));
                        alerts.push(na);
                        (outcome, dispatch)
                    }
                };

                // Update the index to point at whichever alert is now current.
                {
                    let mut by_key = self.by_key.lock().unwrap();
                    let alerts = self.alerts.lock().unwrap();
                    if outcome.is_new {
                        by_key.insert(key, alerts.len() - 1);
                    }
                }

                // Stats + dispatch log.
                {
                    let mut stats = self.stats.lock().unwrap();
                    stats.received_total += 1;
                    if outcome.is_new {
                        stats.new_alerts_total += 1;
                    } else {
                        stats.suppressed_total += 1;
                    }
                }
                if let Some(d) = dispatch {
                    self.push_dispatch(d);
                }

                Ok(outcome)
            }
            None => {
                // Brand-new key.  Build the alert (escalation evaluated while it
                // is still a local, unlocked value) and then publish it.
                let seq = self.next_seq();
                let id = format!("ALT-{seq:08}");
                let mut a = LogicalAlert {
                    id,
                    source: event.source.clone(),
                    category: event.category.clone(),
                    key: key.clone(),
                    base_severity: event.severity,
                    current_severity: event.severity,
                    message: event.message.clone(),
                    context: event.context.clone(),
                    created_ns: now,
                    last_seen_ns: now,
                    occurrences: 1,
                    state: AlertState::Active,
                    ack_ns: None,
                    needs_oncall: false,
                };
                self.escalate(&mut a, now);

                let idx = {
                    let mut alerts = self.alerts.lock().unwrap();
                    let idx = alerts.len();
                    alerts.push(a.clone());
                    idx
                };
                {
                    let mut by_key = self.by_key.lock().unwrap();
                    by_key.insert(key, idx);
                }

                let outcome = SubmitOutcome {
                    alert_id: a.id.clone(),
                    is_new: true,
                    suppressed: false,
                    occurrences: 1,
                    severity: a.current_severity,
                };
                let dispatch = self.dispatch_entry(&a, now);

                {
                    let mut stats = self.stats.lock().unwrap();
                    stats.received_total += 1;
                    stats.new_alerts_total += 1;
                }
                self.push_dispatch(dispatch);

                Ok(outcome)
            }
        }
    }

    fn next_seq(&self) -> u64 {
        let mut s = self.next_seq.lock().unwrap();
        let v = *s;
        *s += 1;
        v
    }

    /// Lazily apply escalation rules to an alert in place.  Returns nothing;
    /// the caller reads `current_severity` / `needs_oncall` afterwards.
    ///
    /// Escalation is frozen once an alert reaches a terminal state: it is
    /// evaluated one final time at acknowledgement (freezing the severity and
    /// on-call flag) and never again.  This keeps reads stable and matches real
    /// operations behaviour — an acknowledged alert does not keep escalating.
    fn escalate(&self, a: &mut LogicalAlert, now: i64) {
        if a.state.is_terminal() {
            return; // frozen at ack time
        }
        let age = now.saturating_sub(a.last_seen_ns);
        match a.current_severity {
            Severity::Warning => {
                if age >= self.cfg.escalation.warning_escalate_after_ns {
                    a.current_severity = Severity::Critical;
                }
            }
            Severity::Critical => {
                if !a.needs_oncall && age >= self.cfg.escalation.critical_escalate_after_ns {
                    a.needs_oncall = true;
                }
            }
            Severity::Info => {}
        }
    }

    /// Build a dispatch-log entry for an alert (used when it is new or escalated).
    fn dispatch_entry(&self, a: &LogicalAlert, ts_ns: i64) -> crate::json::Value {
        crate::json::Value::Object(vec![
            ("alert_id".into(), crate::json::Value::String(a.id.clone())),
            ("source".into(), crate::json::Value::String(a.source.clone())),
            (
                "category".into(),
                crate::json::Value::String(a.category.clone()),
            ),
            (
                "severity".into(),
                crate::json::Value::String(a.current_severity.as_str().to_string()),
            ),
            ("needs_oncall".into(), crate::json::Value::Bool(a.needs_oncall)),
            ("ts_ns".into(), crate::json::Value::Int(ts_ns)),
        ])
    }

    fn push_dispatch(&self, entry: crate::json::Value) {
        let mut log = self.dispatch_log.lock().unwrap();
        log.push_back(entry);
        while log.len() > self.cfg.history.retained {
            log.pop_front();
        }
    }

    // -- acknowledgement ----------------------------------------------------

    /// Acknowledge an alert by id.  Returns the ticket, or `Err(ALT-203)` when
    /// the alert is already resolved, or `Err(ALT-202)` when the id is unknown.
    pub fn acknowledge(&self, alert_id: &str) -> Result<AckTicket, Error> {
        let now = self.clock.now();
        let idx = self.find_index(alert_id).ok_or_else(|| Error::unknown_alert(alert_id))?;

        let ticket = {
            let mut alerts = self.alerts.lock().unwrap();
            let a = &mut alerts[idx];
            if a.state == AlertState::Resolved {
                return Err(Error::already_resolved(alert_id));
            }
            // Evaluate escalation one final time (active -> CRITICAL/oncall if
            // aged) and freeze it: the severity and on-call flag are snapshotted
            // here and never change again, because terminal alerts skip escalate().
            self.escalate(a, now);
            let frozen_severity = a.current_severity;
            let frozen_oncall = a.needs_oncall;
            if a.state != AlertState::Acknowledged {
                a.state = AlertState::Acknowledged;
                a.ack_ns = Some(now);
            }
            // Persist the frozen values so every later read reports them.
            a.current_severity = frozen_severity;
            a.needs_oncall = frozen_oncall;
            let mut stats = self.stats.lock().unwrap();
            stats.acks_total += 1;
            AckTicket {
                alert_id: a.id.clone(),
                state: a.state,
                severity: frozen_severity,
                needs_oncall: frozen_oncall,
                ts_ns: now,
            }
        };
        Ok(ticket)
    }

    /// Resolve (close) an alert by id without requiring an operator.  Used when
    /// the upstream condition clears.  Idempotent for already-resolved alerts.
    pub fn resolve(&self, alert_id: &str) -> Result<AlertState, Error> {
        let idx = self.find_index(alert_id).ok_or_else(|| Error::unknown_alert(alert_id))?;
        let mut alerts = self.alerts.lock().unwrap();
        let a = &mut alerts[idx];
        if a.state != AlertState::Resolved {
            a.state = AlertState::Resolved;
        }
        Ok(a.state)
    }

    fn find_index(&self, alert_id: &str) -> Option<usize> {
        let alerts = self.alerts.lock().unwrap();
        alerts.iter().position(|a| a.id == alert_id)
    }

    // -- queries ------------------------------------------------------------

    /// A single alert by id (with escalation re-evaluated lazily).  `None` if
    /// unknown.
    pub fn get(&self, alert_id: &str) -> Option<LogicalAlert> {
        let now = self.clock.now();
        let mut alerts = self.alerts.lock().unwrap();
        let idx = alerts.iter().position(|a| a.id == alert_id)?;
        self.escalate(&mut alerts[idx], now);
        Some(alerts[idx].clone())
    }

    /// List logical alerts, optionally filtered by source / state / severity,
    /// newest (by `created_ns`) first, bounded by `limit` (0 = all).  Escalation
    /// is re-evaluated for every returned alert.
    pub fn list(
        &self,
        source: Option<&str>,
        state: Option<AlertState>,
        severity: Option<Severity>,
        limit: usize,
    ) -> Vec<LogicalAlert> {
        let now = self.clock.now();
        let mut alerts = self.alerts.lock().unwrap();
        for a in alerts.iter_mut() {
            self.escalate(a, now);
        }
        let mut v: Vec<LogicalAlert> = alerts.clone();
        if let Some(s) = source {
            v.retain(|a| a.source == s);
        }
        if let Some(st) = state {
            v.retain(|a| a.state == st);
        }
        if let Some(sev) = severity {
            v.retain(|a| a.current_severity == sev);
        }
        // Newest first by created_ns (stable: ties keep insertion order).
        v.sort_by(|x, y| y.created_ns.cmp(&x.created_ns));
        if limit > 0 && v.len() > limit {
            v.truncate(limit);
        }
        v
    }

    /// The bounded dispatch log, newest first.  `limit` bounds the result (0 = all).
    pub fn dispatch_log(&self, limit: usize) -> Vec<crate::json::Value> {
        let mut v: Vec<crate::json::Value> = self.dispatch_log.lock().unwrap().iter().cloned().collect();
        v.reverse();
        if limit > 0 && v.len() > limit {
            v.truncate(limit);
        }
        v
    }

    /// A snapshot of the aggregate stats.
    pub fn stats(&self) -> Stats {
        self.stats.lock().unwrap().clone()
    }

    /// Total logical alerts currently tracked (all states).
    pub fn alert_count(&self) -> usize {
        self.alerts.lock().unwrap().len()
    }

    /// Number of alerts in the ACTIVE state (awaiting acknowledgement).
    pub fn active_count(&self) -> usize {
        let now = self.clock.now();
        let mut alerts = self.alerts.lock().unwrap();
        for a in alerts.iter_mut() {
            self.escalate(a, now);
        }
        alerts.iter().filter(|a| a.state == AlertState::Active).count()
    }
}

/// Shared handle to the manager, passed into the HTTP layer.
pub type SharedManager = Arc<AlertManager>;
