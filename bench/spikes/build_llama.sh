#!/bin/sh
# Build the embed worker: llama.cpp at a pinned commit, with the logits patch,
# as a CPU-only variant and (on macOS) a Metal variant. Everything lands under
# bench/data/, which is git-ignored. Needs git, cmake and ninja.
set -eu
COMMIT=c83f305
HERE=$(cd "$(dirname "$0")" && pwd)
DATA="$HERE/../data"
mkdir -p "$DATA" && cd "$DATA"
if [ ! -d llama.cpp ]; then
  git clone --quiet https://github.com/ggml-org/llama.cpp llama.cpp
fi
git -C llama.cpp fetch --quiet origin
git -C llama.cpp checkout --quiet --force "$COMMIT"
git -C llama.cpp apply "$HERE/llama-no-logits.patch"
COMMON="-G Ninja -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=ON -DLLAMA_BUILD_TESTS=OFF -DLLAMA_CURL=OFF"
cmake -S llama.cpp -B llama-build $COMMON -DGGML_METAL=OFF >/dev/null
cmake --build llama-build --target llama-server
if [ "$(uname -s)" = "Darwin" ]; then
  cmake -S llama.cpp -B llama-build-metal $COMMON -DGGML_METAL=ON >/dev/null
  cmake --build llama-build-metal --target llama-server
fi
echo "built: $(ls "$DATA"/llama-build*/bin/llama-server)"
