//! execution_gateway — order lifecycle core.
//!
//! The :struct:`OrderManager` owns all tracked orders and fills behind a
//! `Mutex`.  It is the single source of truth for order state and enforces the
//! legal state machine (NEW -> PARTIALLY_FILLED/FILLED/CANCELED/REJECTED).
//! Venue interaction is simulated in-process: on submission the manager applies
//! the configured reject rate and, by default, fills the order at its limit
//! price, producing a fill record.  All mutation methods return `Result` so the
//! request path never panics.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use crate::config::Config;
use crate::errors::Error;
use crate::models::{Fill, ModifyRequest, Order, OrderState};

/// Thread-safe store of orders and fills.
#[derive(Default)]
pub struct OrderManager {
    cfg: Config,
    orders: Mutex<HashMap<String, Order>>,
    fills: Mutex<Vec<Fill>>,
    next_fill_seq: Mutex<u64>,
    stats: Mutex<Stats>,
}

#[derive(Debug, Default, Clone)]
pub struct Stats {
    pub submitted: u64,
    pub filled: u64,
    pub canceled: u64,
    pub rejected: u64,
    pub modified: u64,
    pub fills_emitted: u64,
}

impl OrderManager {
    pub fn new(cfg: Config) -> Self {
        OrderManager {
            cfg,
            orders: Mutex::new(HashMap::new()),
            fills: Mutex::new(Vec::new()),
            next_fill_seq: Mutex::new(1),
            stats: Mutex::new(Stats::default()),
        }
    }

    // -- lifecycle ----------------------------------------------------------

    /// Submit a new order.  Applies the simulated venue decision and, on
    /// acceptance, fills it at the limit price.  Returns the resulting order.
    pub fn submit(&self, mut order: Order, now_ns: i64) -> Result<Order, Error> {
        // basic validation against configured caps
        if order.qty < 1 {
            return Err(Error::bad_request(format!("qty must be >= 1, got {}", order.qty)));
        }
        if order.qty > self.cfg.lifecycle.max_order_qty {
            return Err(Error::bad_request(format!(
                "qty {} exceeds max_order_qty {}",
                order.qty, self.cfg.lifecycle.max_order_qty
            )));
        }
        if order.limit_price < self.cfg.lifecycle.min_price {
            return Err(Error::bad_request(format!(
                "limit_px {} below min_price {}",
                order.limit_price, self.cfg.lifecycle.min_price
            )));
        }

        // simulated venue rejection
        if self.venue_rejects() {
            order.state = OrderState::Rejected;
            order.updated_ns = now_ns;
            bump_stat(&self.stats, |s| s.rejected += 1);
            self.orders.lock().unwrap().insert(order.id.clone(), order.clone());
            return Ok(order);
        }

        // simulated full fill at the limit price
        order.filled_qty = order.qty;
        order.state = OrderState::Filled;
        order.updated_ns = now_ns;
        self.record_fill(&order, order.qty, order.limit_price, now_ns)?;
        bump_stat(&self.stats, |s| s.filled += 1);

        self.orders.lock().unwrap().insert(order.id.clone(), order.clone());
        Ok(order)
    }

    /// Modify an open order's quantity and/or limit price.
    pub fn modify(&self, id: &str, req: &ModifyRequest, now_ns: i64) -> Result<Order, Error> {
        let mut orders = self.orders.lock().unwrap();
        let order = orders.get_mut(id).ok_or_else(|| Error::unknown_order(id))?;
        if !order.state.is_open() {
            return Err(Error::invalid_state_transition(
                id,
                order.state.as_str(),
                "MODIFY",
            ));
        }
        if let Some(qty) = req.qty {
            if qty < 1 {
                return Err(Error::bad_request("modify qty must be >= 1"));
            }
            if qty <= order.filled_qty {
                return Err(Error::invalid_state_transition(
                    id,
                    "open",
                    &format!("reduce below filled {}", order.filled_qty),
                ));
            }
            order.qty = qty;
        }
        if let Some(px) = req.limit_price {
            if px < self.cfg.lifecycle.min_price {
                return Err(Error::bad_request("modify limit_px below min_price"));
            }
            order.limit_price = px;
        }
        order.updated_ns = now_ns;
        drop(orders);

        bump_stat(&self.stats, |s| s.modified += 1);
        Ok(self.get(id)?)
    }

    /// Cancel an open order.
    pub fn cancel(&self, id: &str, now_ns: i64) -> Result<Order, Error> {
        let mut orders = self.orders.lock().unwrap();
        let order = orders.get_mut(id).ok_or_else(|| Error::unknown_order(id))?;
        if !order.state.is_open() {
            return Err(Error::invalid_state_transition(
                id,
                order.state.as_str(),
                "CANCELED",
            ));
        }
        order.state = OrderState::Canceled;
        order.updated_ns = now_ns;
        drop(orders);

        bump_stat(&self.stats, |s| s.canceled += 1);
        Ok(self.get(id)?)
    }

    // -- queries ------------------------------------------------------------

    pub fn get(&self, id: &str) -> Result<Order, Error> {
        self.orders
            .lock()
            .unwrap()
            .get(id)
            .cloned()
            .ok_or_else(|| Error::unknown_order(id))
    }

    /// All orders, newest first.  `limit` bounds the result (0 = all).
    pub fn list(&self, limit: usize) -> Vec<Order> {
        let mut v: Vec<Order> = self.orders.lock().unwrap().values().cloned().collect();
        v.sort_by_key(|o| o.created_ns);
        v.reverse();
        if limit > 0 && v.len() > limit {
            v.truncate(limit);
        }
        v
    }

    /// Recent fills, newest first.
    pub fn fills(&self, limit: usize) -> Vec<Fill> {
        let mut v: Vec<Fill> = self.fills.lock().unwrap().clone();
        v.reverse();
        if limit > 0 && v.len() > limit {
            v.truncate(limit);
        }
        v
    }

    pub fn stats(&self) -> Stats {
        self.stats.lock().unwrap().clone()
    }

    /// Number of currently open (NEW/PARTIALLY_FILLED) orders.
    pub fn open_count(&self) -> usize {
        self.orders
            .lock()
            .unwrap()
            .values()
            .filter(|o| o.state.is_open())
            .count()
    }

    // -- internals ----------------------------------------------------------

    /// Deterministic pseudo-random reject based on the configured rate.  Uses a
    /// simple LCG seeded by time so behavior is stable within a process but
    /// non-trivial across runs; at rate 0 it never rejects.
    fn venue_rejects(&self) -> bool {
        let rate = self.cfg.venue.simulated_reject_rate;
        if rate <= 0.0 {
            return false;
        }
        if rate >= 1.0 {
            return true;
        }
        // splitmix-style hash of the current nanosecond clock as a cheap PRNG
        let seed = crate::models::now_ns() as u64;
        let mut x = seed.wrapping_add(0x9e3779b97f4a7c15);
        x = (x ^ (x >> 30)).wrapping_mul(0xbf58476d1ce4e5b9);
        x = (x ^ (x >> 27)).wrapping_mul(0x94d049bb133111eb);
        x ^= x >> 31;
        // map to [0,1)
        let r = (x as f64) / (u64::MAX as f64);
        r < rate
    }

    fn record_fill(
        &self,
        order: &Order,
        qty: i64,
        price: f64,
        ts_ns: i64,
    ) -> Result<Fill, Error> {
        let seq = {
            let mut s = self.next_fill_seq.lock().unwrap();
            let v = *s;
            *s += 1;
            v
        };
        let fill = Fill {
            id: format!("FILL-{seq:08}"),
            order_id: order.id.clone(),
            symbol: order.symbol.clone(),
            venue: order.venue.clone(),
            side: order.side,
            qty,
            price,
            ts_ns,
        };
        let mut fills = self.fills.lock().unwrap();
        fills.push(fill.clone());
        while fills.len() > self.cfg.lifecycle.fills_retained {
            fills.remove(0);
        }
        drop(fills);
        bump_stat(&self.stats, |s| s.fills_emitted += 1);
        Ok(fill)
    }
}

/// Increment a single stats field under the lock (avoids cloning the struct).
fn bump_stat(stats: &Mutex<Stats>, f: impl FnOnce(&mut Stats)) {
    let mut s = stats.lock().unwrap();
    f(&mut s);
}

/// Shared handle to the manager, passed into the HTTP layer.
pub type SharedManager = Arc<OrderManager>;
