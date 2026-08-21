# Handwritten Makefile build profile (PR1: multi-build-system integration).
#
# Builds the SAME source files the root Bazel build does -- referenced
# directly from ../../{include,src,strings_lib,consumer}, never copied --
# into libmath.so, libstrings.so, and the consumer_app executable,
# mirroring BUILD.bazel/strings_lib/BUILD.bazel/consumer/BUILD.bazel
# target-for-target. PR1: advisory/shadow only (ci/profiles.yaml's
# linux-x86_64-gcc14-cxx17-make-bear profile has contract: false) -- no
# ABICheck scan runs against this build yet.
#
# CC/CXX default to gcc-14/g++-14 (see ci/profiles.yaml's environment
# note); override on the command line (`make CC=gcc CXX=g++`) for a local
# toolchain that doesn't have gcc-14 installed.

LAB_ROOT   := $(abspath ../..)
BUILD_DIR  := build

# Plain `?=` does NOT work here: GNU Make ships built-in default values for
# CC/CXX (origin "default") from its implicit-rules database, so `?=` (which
# only assigns when a variable is completely undefined) silently no-ops and
# the build falls back to whatever `g++`/`cc` resolve to on PATH -- verified
# directly: `?=` alone built with plain `g++`, not `g++-14`, on this
# environment. Only override the *default* origin, so an explicit
# `make CC=gcc CXX=g++` (origin "command line") or `CXX=g++ make`
# (origin "environment") still takes precedence over the gcc-14/g++-14
# pin, exactly like `?=` was supposed to behave.
ifeq ($(origin CC),default)
CC := gcc-14
endif
ifeq ($(origin CXX),default)
CXX := g++-14
endif
CXXSTD     := -std=c++17
CXXFLAGS   := $(CXXSTD) -fPIC -O2 -g -Wall
CPPFLAGS_MATH    := -I$(LAB_ROOT)/include
CPPFLAGS_STRINGS := -I$(LAB_ROOT)/strings_lib/include

MATH_SRC     := $(LAB_ROOT)/src/math.cc
STRINGS_SRC  := $(LAB_ROOT)/strings_lib/src/strings.cc
CONSUMER_SRC := $(LAB_ROOT)/consumer/app.cc

LIBMATH     := $(BUILD_DIR)/libmath.so
LIBSTRINGS  := $(BUILD_DIR)/libstrings.so
CONSUMER    := $(BUILD_DIR)/consumer_app

.PHONY: all clean compiledb smoke-test

all: $(LIBMATH) $(LIBSTRINGS) $(CONSUMER)

$(BUILD_DIR):
	mkdir -p $(BUILD_DIR)

# No -Wl,-soname (deliberately, no longer the "-shared with no SONAME"
# default this comment used to warn against): the cross_build_equivalence
# job (ci/compare_build_outputs.py) found a real public_contract_mismatch
# between all three build systems here -- this Makefile previously set an
# unversioned SONAME equal to the bare filename (-Wl,-soname,libmath.so),
# CMake's own SOVERSION set a versioned one (libmath.so.1), and Bazel's
# `math` cc_binary(linkshared=True) target embeds no SONAME at all.
# BUILD.bazel's own comment keeps `math` deliberately unchanged (every
# existing job's cache key, evidence pack, and committed baseline are
# keyed to it, and it's the required abi-scan.yml gate's own target), so
# this build aligns to Bazel's existing no-SONAME shape instead of adding
# SONAME handling to the required gate's own target.
$(LIBMATH): $(MATH_SRC) | $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) $(CPPFLAGS_MATH) -shared \
	    -o $@ $<

$(LIBSTRINGS): $(STRINGS_SRC) | $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) $(CPPFLAGS_STRINGS) -shared \
	    -o $@ $<

# Dynamically links against libmath.so (mirrors consumer/BUILD.bazel's
# cc_import wrapping) rather than recompiling math.cc into the binary.
# rpath covers both layouts: $ORIGIN for the build tree (consumer_app and
# libmath.so colocated in build/), $ORIGIN/../lib for ci/emit_build_output.py's
# staged layout (artifacts/bin/consumer_app one directory from
# artifacts/lib/libmath.so) -- $ORIGIN alone only resolved the former,
# leaving a staged-only consumer_app unable to load its dependency
# (CodeRabbit review, PR #19).
$(CONSUMER): $(CONSUMER_SRC) $(LIBMATH) | $(BUILD_DIR)
	$(CXX) $(CXXSTD) -I$(LAB_ROOT)/include \
	    -o $@ $< -L$(BUILD_DIR) -lmath -Wl,-rpath,'$$ORIGIN:$$ORIGIN/../lib'

# Optional compile_commands.json via `bear` (PR1 item 5's own requirement:
# invoke it if present, degrade gracefully -- never fail hard -- if it
# isn't). See ci/backends/make.py's own handling of this same gap.
compiledb:
	@if command -v bear >/dev/null 2>&1; then \
	    $(MAKE) clean; \
	    mkdir -p $(BUILD_DIR); \
	    bear --output $(BUILD_DIR)/compile_commands.json -- $(MAKE) all; \
	else \
	    echo "bear not found on PATH -- skipping compile_commands.json generation" >&2; \
	fi

smoke-test: all
	LD_LIBRARY_PATH=$(BUILD_DIR) $(BUILD_DIR)/consumer_app

clean:
	rm -rf $(BUILD_DIR)
