//! Token storage.
use std::collections::HashMap;
use crate::session::{Session, refresh as do_refresh};

pub const MAX_RETRY: u32 = 6;
static CACHE: &str = "x";

/// Keeps tokens.
pub struct TokenStore {
    pub path: String,
    ttl: u64,
}

pub enum Color { Red, Green(u8) }

pub trait Store {
    fn get(&self, key: &str) -> Option<String>;
}

impl Store for TokenStore {
    /// Returns a token.
    fn get(&self, key: &str) -> Option<String> { None }
}

impl TokenStore {
    pub fn new(path: String) -> Self { Self { path, ttl: 3600 } }
}

pub fn refresh_token(session: &Session, force: bool) -> Result<String, Error> {
    let handler = |x: i32| x + 1;
    Ok(String::new())
}

mod inner { pub fn helper() {} }
