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
# Deliberately NO -Wl,-soname. The reference these fixtures are compared
# against is the Bazel path, whose `cc_binary(linkshared = True)` emits no
# SONAME -- so the oracle's expected_gating_symbols contains only API
# symbols. CMake sets a SONAME for a SHARED library by default and a bare
# `g++ -shared` does not, so an unadjusted pair of recipes gives all three
# build systems a different artifact shape and ABICheck reports an extra
# `soname_bump_recommended`/DT_SONAME finding on whichever ones have one.
#
# An earlier revision of this file added -Wl,-soname to match CMake. That
# aligned Make with CMake and moved BOTH away from Bazel, turning a
# two-way agreement into a three-way disagreement that only CI could see.
# buildsystems/cmake/fixtures/CMakeLists.txt now suppresses its SONAME for
# the same reason: all three recipes emit the reference shape.
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
DEP := $(BUILD_DIR)/libimpl.d

# roadmap.md item 12's own "what remains": fixtures/generated_header/'s
# lib.h is NOT a checked-in source file -- a Bazel genrule
# (fixtures/generated_header/gen_header.py) produces it at build time, so
# there is nothing under FIXTURE_DIR for the plain -I"$(FIXTURE_DIR)"
# below to find. A fixture opts into generation by shipping
# FIXTURE_DIR/header_functions.txt (one exported function name per line --
# tests/test_generated_header_parity.py cross-checks this against
# fixtures/generated_header/v{1,2}/BUILD.bazel's own genrule `cmd` so the
# two can't silently diverge). lib.cc's own #include is a full
# repo-relative path (see that fixture's own lib.cc comment for why a
# bare filename doesn't resolve for a build-generated header), so this
# mirrors that same repo-relative path under BUILD_DIR/generated_root and
# adds ITS PARENT as an extra -I -- lib.cc itself needs no change and
# stays byte-identical across all three build systems.
REPO_ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST)))../../..)
GEN_MARKER := $(wildcard $(FIXTURE_DIR)/header_functions.txt)
FIXTURE_DIR_REL := $(patsubst $(REPO_ROOT)/%,%,$(abspath $(FIXTURE_DIR)))

ifneq ($(GEN_MARKER),)
GEN_ROOT := $(BUILD_DIR)/generated_root
GEN_HEADER := $(GEN_ROOT)/$(FIXTURE_DIR_REL)/lib.h
GEN_SCRIPT := $(dir $(FIXTURE_DIR))gen_header.py
INCLUDE_EXTRA := -I$(GEN_ROOT)

$(GEN_HEADER): $(GEN_MARKER) $(GEN_SCRIPT)
	@mkdir -p "$(dir $@)"
	python3 "$(GEN_SCRIPT)" "$@" $$(cat "$(GEN_MARKER)")
else
GEN_HEADER :=
INCLUDE_EXTRA :=
endif

# Make's default goal is otherwise whichever real (non-.PHONY) rule is
# textually FIRST in the file -- the $(GEN_HEADER) rule above, when a
# fixture opts into generation, which would make a bare `make ... ` build
# only the header and stop. Pinned explicitly so `all` (-> $(OUT), the
# actual shared library) stays the default regardless of rule order.
.DEFAULT_GOAL := all

.PHONY: all clean
all: $(OUT)

# Codex review: lib.cc was this target's ONLY prerequisite, so a changed
# lib.h left libimpl.so looking up to date and make skipped the rebuild.
# That is reachable with the default arguments -- run_scenario.py's scratch
# directory is `<results-dir>/make-scratch`, and --results-dir defaults to
# the persistent `scenario-results`, so a second run after editing a
# fixture header compared a STALE binary against the new header and
# reported findings for the previous pair.
#
# Worse than a plain staleness bug: CMake and Bazel both track header
# dependencies on their own, so only the Make leg would go stale, and
# check_scenario_parity.py would report that as a cross-build-system
# DISAGREEMENT -- a false positive in the gate whose entire job is telling
# real disagreements from noise.
#
# -MMD -MP has the compiler emit the real prerequisite list, so this
# tracks every header the translation unit actually includes rather than
# just lib.h -- a fixture that grows a second header stays correct with no
# change here. -MP adds phony targets for each header so a DELETED header
# does not wedge make with "no rule to make target". The `-include` is
# leading-dash on purpose: the .d file does not exist on the first build,
# and that is not an error.
# Codex review, second pass on this target: -MMD tracks the headers of the
# fixture that was built, but OUT and DEP are keyed on BUILD_DIR alone while
# SRC is keyed on FIXTURE_DIR. run_scenario.py names its build dir after the
# SCENARIO (`<scratch>/<name>-old`) and takes the fixture from
# scenarios/build-matrix.yaml, so re-pointing a scenario at a different
# fixture reuses the same build dir with a different source. Fixture files
# are checked-out sources whose mtimes predate the previous build, so make
# saw "Nothing to be done" and left the PREVIOUS FIXTURE's libimpl.so in
# place -- a wrong-fixture comparison, worse than the stale-header case
# above, and equally invisible because CMake and Bazel would not do it.
#
# The stamp records which fixture the build dir currently holds and is
# rewritten only when that value actually changes (cmp before write), so
# incremental rebuilds within one fixture stay incremental.
STAMP := $(BUILD_DIR)/libimpl.fixture

.PHONY: force
$(STAMP): force
	@mkdir -p "$(BUILD_DIR)"
	@printf '%s\n' "$(FIXTURE_DIR)" | cmp -s - "$@" 2>/dev/null || printf '%s\n' "$(FIXTURE_DIR)" > "$@"

$(OUT): $(SRC) $(STAMP) $(GEN_HEADER)
	@test -f "$(SRC)" || { echo "FIXTURE_DIR=$(FIXTURE_DIR) has no lib.cc (expected the fixtures/<name>/{v1,v2} shape)" >&2; exit 1; }
	@mkdir -p "$(BUILD_DIR)"
	$(CXX) -std=$(CXXSTD) -O2 -g -fPIC -shared $(INCLUDE_EXTRA) -I"$(FIXTURE_DIR)" \
	    -MMD -MP -MF "$(DEP)" "$(SRC)" -o "$(OUT)"

-include $(DEP)

clean:
	rm -f "$(OUT)" "$(DEP)" "$(STAMP)"
