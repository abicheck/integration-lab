#!/usr/bin/env bash
set +e
ROOT=/tmp/icx-libcxx
ICX=/opt/intel/oneapi/compiler/2026.1/bin/icx
ICPX=/opt/intel/oneapi/compiler/2026.1/bin/icpx
LLVM=/usr/lib/llvm-22
export PATH="/opt/intel/oneapi/compiler/2026.1/bin:$PATH"
export LD_LIBRARY_PATH="/opt/intel/oneapi/compiler/2026.1/lib:${LD_LIBRARY_PATH:-}"
mkdir -p "$ROOT/logs" "$ROOT/results"

cmake -S "$GITHUB_WORKSPACE/abicheck/contrib/abicheck-clang-plugin" -B "$ROOT/build" -G Ninja \
  -DCMAKE_C_COMPILER="$ICX" -DCMAKE_CXX_COMPILER="$ICPX" \
  -DLLVM_DIR="$LLVM/lib/cmake/llvm" -DClang_DIR="$LLVM/lib/cmake/clang" \
  -DCMAKE_CXX_FLAGS='-fno-rtti -stdlib=libc++' -DCMAKE_BUILD_TYPE=Release \
  >"$ROOT/logs/build.log" 2>&1
configure_rc=$?
build_rc=125
if [[ $configure_rc = 0 ]]; then
  cmake --build "$ROOT/build" -v >>"$ROOT/logs/build.log" 2>&1
  build_rc=$?
fi
printf 'configure=%s\nbuild=%s\n' "$configure_rc" "$build_rc" >"$ROOT/results/build.status"
cat "$ROOT/logs/build.log"
plugin="$ROOT/build/libabicheck-facts.so"
ldd "$plugin" || true
nm -D --undefined-only "$plugin" | c++filt | grep -E 'PluginASTAction|std::__1|std::__cxx11' | head -100 || true

mkdir -p "$ROOT/fixture/include"
cat >"$ROOT/fixture/include/api.hpp" <<'EOF'
#pragma once
namespace sample { struct Widget { int value; }; inline int get(Widget w) { return w.value; } }
EOF
cat >"$ROOT/fixture/use.cpp" <<'EOF'
#include "api.hpp"
int use_widget(sample::Widget w) { return sample::get(w); }
EOF

out="$ROOT/smoke"
"$ICPX" -std=c++17 -I"$ROOT/fixture/include" -fplugin="$plugin" \
  -Xclang -plugin-arg-abicheck-facts -Xclang "out=$out" \
  -Xclang -plugin-arg-abicheck-facts -Xclang "public-roots=$ROOT/fixture/include" \
  -c "$ROOT/fixture/use.cpp" -o "$ROOT/use.o" >"$ROOT/logs/smoke.log" 2>&1
smoke_rc=$?
echo "rc=$smoke_rc" >"$ROOT/results/smoke.status"
cat "$ROOT/logs/smoke.log"
find "$out" -type f -maxdepth 3 -print 2>/dev/null || true

python3 -m venv "$ROOT/venv"
source "$ROOT/venv/bin/activate"
pip install -q -e "$GITHUB_WORKSPACE/abicheck"
conformance_rc=125
if [[ $smoke_rc = 0 ]]; then
  python "$GITHUB_WORKSPACE/abicheck/contrib/abicheck-clang-plugin/tests/conformance.py" \
    --plugin "$plugin" --clangxx "$ICPX" --work "$ROOT/conf" --keep \
    >"$ROOT/logs/conformance.log" 2>&1
  conformance_rc=$?
else
  echo "skipped smoke=$smoke_rc" >"$ROOT/logs/conformance.log"
fi
echo "rc=$conformance_rc" >"$ROOT/results/conformance.status"
cat "$ROOT/logs/conformance.log"

sycl_rc=125
if [[ $smoke_rc = 0 ]]; then
  "$ICPX" -fsycl -std=c++17 -I"$ROOT/fixture/include" -fplugin="$plugin" \
    -Xclang -plugin-arg-abicheck-facts -Xclang "out=$ROOT/sycl" \
    -Xclang -plugin-arg-abicheck-facts -Xclang "public-roots=$ROOT/fixture/include" \
    -fsyntax-only "$ROOT/fixture/use.cpp" >"$ROOT/logs/sycl.log" 2>&1
  sycl_rc=$?
else
  echo "skipped smoke=$smoke_rc" >"$ROOT/logs/sycl.log"
fi
echo "rc=$sycl_rc" >"$ROOT/results/sycl.status"
cat "$ROOT/logs/sycl.log"
exit 0
