# The third build system's fixture recipe, the Make counterpart of
# buildsystems/cmake/fixtures/CMakeLists.txt: ONE generic makefile
# parameterized by FIXTURE_DIR, not one per fixture.
#
# Every scenario fixture under fixtures/ has the same shape (one lib.cc,
# one lib.h, a shared object the scenario runner compares), so a single
# recipe covers every scenario scripts/run_scenario.py's
# --build-system make path declares in scenarios/build-matrix.yaml.
#
# Output name is fixed at libimpl.so, matching both the Bazel path
# (bazel-bin/fixtures/<name>/<version>/libimpl.so) and the CMake path's
# own OUTPUT_NAME, so run_scenario.py compares the same filename whichever
# build system produced it.
#
# -Wl,-soname is load-bearing, not cosmetic. CMake sets SONAME for a SHARED
# library by default and so does Bazel's cc_library; a bare `g++ -shared`
# does not. Without it this recipe produced an artifact that differed from
# the other two build systems' in a way ABICheck notices -- it reports
# `soname_bump_recommended` on a breaking change only when there is a
# SONAME to bump -- so three build systems each passed their own oracle
# while disagreeing with each other. scripts/check_scenario_parity.py
# caught exactly that on its first run; this is the fix.
#
# CXX is pinned by the caller (run_scenario.py passes CXX=g++-14), matching
# ci/profiles.yaml's "no silent fallback to whatever gcc is on PATH"
# rule. The default here is deliberately the same pinned executable rather
# than plain c++, so a hand-run of this makefile does not quietly use a
# different compiler than CI does.
CXX ?= g++-14
CXXSTD ?= c++17
BUILD_DIR ?= .

ifndef FIXTURE_DIR
$(error FIXTURE_DIR must be set: make -f buildsystems/make/fixtures/Makefile FIXTURE_DIR=fixtures/<name>/v1 BUILD_DIR=<dir>)
endif

SRC := $(FIXTURE_DIR)/lib.cc
OUT := $(BUILD_DIR)/libimpl.so

.PHONY: all clean
all: $(OUT)

$(OUT): $(SRC)
	@test -f "$(SRC)" || { echo "FIXTURE_DIR=$(FIXTURE_DIR) has no lib.cc (expected the fixtures/<name>/{v1,v2} shape)" >&2; exit 1; }
	@mkdir -p "$(BUILD_DIR)"
	$(CXX) -std=$(CXXSTD) -O2 -g -fPIC -shared -Wl,-soname,libimpl.so \
	  -I"$(FIXTURE_DIR)" "$(SRC)" -o "$(OUT)"

clean:
	rm -f "$(OUT)"
