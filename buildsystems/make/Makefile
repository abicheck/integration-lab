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
CPPFLAGS_CORE    := -I$(LAB_ROOT)/core_include
CPPFLAGS_STRINGS := -I$(LAB_ROOT)/strings_lib/include
CPPFLAGS_INTERNAL := -I$(LAB_ROOT)/internal/include

MATH_SRC     := $(LAB_ROOT)/src/math.cc
CORE_SRC     := $(LAB_ROOT)/src/core.cc
STRINGS_SRC  := $(LAB_ROOT)/strings_lib/src/strings.cc
CONSUMER_SRC := $(LAB_ROOT)/consumer/app.cc

LIBMATH     := $(BUILD_DIR)/libmath.so
LIBCORE     := $(BUILD_DIR)/libcore.so
LIBSTRINGS  := $(BUILD_DIR)/libstrings.so
CONSUMER    := $(BUILD_DIR)/consumer_app

.PHONY: all clean compiledb smoke-test

all: $(LIBCORE) $(LIBMATH) $(LIBSTRINGS) $(CONSUMER)

$(BUILD_DIR):
	mkdir -p $(BUILD_DIR)

$(LIBCORE): $(CORE_SRC) | $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) $(CPPFLAGS_CORE) $(CPPFLAGS_INTERNAL) -shared \
	    -Wl,-soname,libcore.so \
	    -o $@ $<

# -Wl,-soname,libmath.so: an explicit SONAME equal to this library's own
# filename -- a real shared library should carry a SONAME for correct
# dynamic-linker identity. Same string BUILD.bazel's own `math` linkopts
# and CMakeLists.txt's own (default) SONAME embed -- all three build
# systems need the identical SONAME for cross_build_equivalence
# (ci/compare_build_outputs.py) to actually agree, not just each
# independently "have a SONAME" (see those two files' own comments).
$(LIBMATH): $(MATH_SRC) $(LIBCORE) | $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) $(CPPFLAGS_MATH) $(CPPFLAGS_INTERNAL) -shared \
	    -Wl,-soname,libmath.so \
	    -o $@ $< -L$(BUILD_DIR) -Wl,--no-as-needed -lcore -Wl,--as-needed,-z,now,-rpath,'$$ORIGIN'

$(LIBSTRINGS): $(STRINGS_SRC) $(LIBCORE) | $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) $(CPPFLAGS_STRINGS) $(CPPFLAGS_INTERNAL) -shared \
	    -Wl,-soname,libstrings.so \
	    -o $@ $< -L$(BUILD_DIR) -Wl,--no-as-needed -lcore -Wl,--as-needed,-z,now,-rpath,'$$ORIGIN'

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

# The native Make contract requires compile_commands.json.  Calling Bear
# directly deliberately makes a missing tool or failed capture fatal rather
# than allowing a source-evidence check to degrade to a shallow green result.
compiledb:
	$(MAKE) clean
	mkdir -p $(BUILD_DIR)
	bear --output $(BUILD_DIR)/compile_commands.json -- $(MAKE) all

smoke-test: all
	LD_LIBRARY_PATH=$(BUILD_DIR) $(BUILD_DIR)/consumer_app

clean:
	rm -rf $(BUILD_DIR)
