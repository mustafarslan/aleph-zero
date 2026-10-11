// Package auth stores tokens.
package auth

import (
	"fmt"
	str "strings"
)

const MaxRetry = 6

var cache = map[string]string{}

// TokenStore keeps tokens.
type TokenStore struct {
	Path string
	ttl  int
}

type Store interface {
	Get(key string) (string, bool)
}

// Get returns a token.
func (s *TokenStore) Get(key string) (string, bool) {
	v, ok := cache[key]
	return str.TrimSpace(v), ok
}

func RefreshToken(a, b int, name string) error {
	handler := func(x int) int { return x + 1 }
	fmt.Println(handler(a))
	return nil
}
