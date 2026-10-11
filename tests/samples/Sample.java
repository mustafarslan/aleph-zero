package com.example.auth;

import java.util.Map;
import static java.util.Objects.requireNonNull;
import com.example.session.*;

/** Keeps tokens. */
public class TokenStore extends Base implements Store {
    public static final int MAX_RETRY = 6;
    private final Map<String, String> cache;

    public TokenStore(Map<String, String> cache) { this.cache = requireNonNull(cache); }

    /** Returns a token. */
    @Override
    public String get(String key) { return cache.get(key); }

    interface Store { String get(String key); }
    enum Color { RED, GREEN }
    record Pair(String a, int b) {}
}
