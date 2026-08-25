# CMake build profile (PR1: multi-build-system integration).
#
# Builds the SAME source files the root Bazel build does -- referenced
# directly from ../../{include,src,strings_lib,consumer}, never copied --
# into a math shared library, a strings shared library, and a consumer
# executable, mirroring BUILD.bazel/consumer/BUILD.bazel/strings_lib/BUILD.bazel
# target-for-target. This is PR1: advisory/shadow only (ci/profiles.yaml's
# linux-x86_64-gcc14-cxx17-cmake-ninja profile has contract: false) -- no
# ABICheck scan runs against this build yet, it only proves the source
# builds cleanly under a second build system and stages canonical output
# (see ci/emit_build_output.py).
cmake_minimum_required(VERSION 3.20)
project(abicheck_lab_cmake CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)
set(CMAKE_BUILD_RPATH_USE_ORIGIN TRUE)

# Always emit compile_commands.json (PR1 item 4's own requirement) --
# redundant with CMAKE_EXPORT_COMPILE_COMMANDS in CMakePresets.json, set
# again here so a plain `cmake -S . -B build` (no preset) still gets one.
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

# Repo root, two levels up from buildsystems/cmake/.
get_filename_component(LAB_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../.." ABSOLUTE)

add_library(core SHARED "${LAB_ROOT}/src/core.cc")
target_include_directories(core PUBLIC "${LAB_ROOT}/core_include")
target_include_directories(core PRIVATE "${LAB_ROOT}/internal/include")
set_target_properties(core PROPERTIES OUTPUT_NAME "core")

# ---- math -------------------------------------------------------------
# Mirrors BUILD.bazel's `math` cc_binary(linkshared = True): same
# src/math.cc, same public header include/abicheck_lab/math.h.
#
# No SOVERSION/VERSION set, and NO_SONAME is left at its CMake default
# (FALSE): CMake gives a shared library target an SONAME equal to its own
# OUTPUT_NAME by default when neither SOVERSION nor NO_SONAME is set --
# verified directly against a real local build (`readelf -d
# libmath.so | grep SONAME` -> "libmath.so"), no versioned symlink
# involved. Deliberately NOT using SOVERSION/VERSION here: that would
# produce a *different* SONAME (libmath.so.1) than Bazel's and Make's own
# builds embed (see BUILD.bazel's/Makefile's own linkopts/-Wl,-soname
# comments) -- all three build systems need the identical SONAME string
# for cross_build_equivalence (ci/compare_build_outputs.py) to actually
# agree, not just each independently "have a SONAME".
add_library(math SHARED "${LAB_ROOT}/src/math.cc")
target_include_directories(math PUBLIC "${LAB_ROOT}/include")
target_include_directories(math PRIVATE "${LAB_ROOT}/internal/include")
target_link_libraries(math PRIVATE core)
target_link_options(math PRIVATE "LINKER:--no-as-needed,-u,abicheck_lab_core_scale")
set_target_properties(math PROPERTIES
    OUTPUT_NAME "math"
)

# ---- strings ------------------------------------------------------------
# Mirrors strings_lib/BUILD.bazel's `strings` cc_binary(linkshared = True).
# CMake's default SONAME (== OUTPUT_NAME) -- same reasoning as `math` above.
add_library(strings SHARED "${LAB_ROOT}/strings_lib/src/strings.cc")
target_include_directories(strings PUBLIC "${LAB_ROOT}/strings_lib/include")
target_include_directories(strings PRIVATE "${LAB_ROOT}/internal/include")
target_link_libraries(strings PRIVATE core)
target_link_options(strings PRIVATE "LINKER:--no-as-needed,-u,abicheck_lab_core_scale")
set_target_properties(strings PROPERTIES
    OUTPUT_NAME "strings"
)

# Transitional artifacts for the lab-owned profile baselines.  Those
# baselines predate the native project contract and intentionally retain
# their historical SONAMEs; the standardized project artifacts above use
# the cross-build-system SONAME contract instead.
configure_file("${LAB_ROOT}/src/math.cc" "${CMAKE_CURRENT_BINARY_DIR}/legacy_math.cc" COPYONLY)
configure_file("${LAB_ROOT}/strings_lib/src/strings.cc" "${CMAKE_CURRENT_BINARY_DIR}/legacy_strings.cc" COPYONLY)
add_library(legacy_math SHARED "${CMAKE_CURRENT_BINARY_DIR}/legacy_math.cc")
target_include_directories(legacy_math PUBLIC "${LAB_ROOT}/include")
target_link_options(legacy_math PRIVATE "LINKER:-soname,libmath.so.1")
set_target_properties(legacy_math PROPERTIES NO_SONAME TRUE)

add_library(legacy_strings SHARED "${CMAKE_CURRENT_BINARY_DIR}/legacy_strings.cc")
target_include_directories(legacy_strings PUBLIC "${LAB_ROOT}/strings_lib/include")
target_link_options(legacy_strings PRIVATE "LINKER:-soname,libstrings.so.1")
set_target_properties(legacy_strings PROPERTIES NO_SONAME TRUE)

# ---- consumer_app ---------------------------------------------------------
# Mirrors consumer/BUILD.bazel's `consumer_app` cc_binary: links dynamically
# against `math` (never recompiles math.cc into the binary itself), same as
# consumer/BUILD.bazel's cc_import wrapping.
add_executable(consumer_app "${LAB_ROOT}/consumer/app.cc")
target_include_directories(consumer_app PRIVATE "${LAB_ROOT}/include")
target_link_libraries(consumer_app PRIVATE math)

# Without an explicit RPATH, CMake's default build-tree RPATH is an
# *absolute* path back into buildsystems/cmake/build/ (visible via `readelf
# -d consumer_app`), which only resolves from that exact build tree -- the
# staged copy (ci/emit_build_output.py's artifacts/bin/consumer_app +
# artifacts/lib/libmath.so*, one directory apart) can't load its dependency
# once the original build directory is gone (CodeRabbit review, PR #19).
# $ORIGIN covers the build tree (consumer_app and libmath.so* colocated in
# build/); $ORIGIN/../lib covers the staged layout.
set_target_properties(consumer_app PROPERTIES
    BUILD_RPATH "$ORIGIN:$ORIGIN/../lib"
)

# Smoke-testable target list this profile's backend/driver expects to find
# under the build directory: libmath.so, libstrings.so, consumer_app.
