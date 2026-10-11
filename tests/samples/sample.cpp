#include <string>
#include "session.h"

namespace auth {

constexpr int kMaxRetry = 6;
static std::string g_cache;

/// Keeps tokens.
class TokenStore : public Base {
 public:
  explicit TokenStore(std::string path);
  ~TokenStore();
  std::string Get(const std::string& key) const;
  static int instances;
 private:
  int ttl_ = 3600;
};

struct Pair { int a; int b; };
enum class Color { Red, Green };

template <typename T>
T Identity(T value) { return value; }

std::string TokenStore::Get(const std::string& key) const {
  auto handler = [](int x) { return x + 1; };
  return g_cache;
}

int RefreshToken(Session* session, bool force);

}  // namespace auth
#define MAX_BUF 1024
typedef unsigned long ulong_t;
using Id = int;
