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

# Always emit compile_commands.json (PR1 item 4's own requirement) --
# redundant with CMAKE_EXPORT_COMPILE_COMMANDS in CMakePresets.json, set
# again here so a plain `cmake -S . -B build` (no preset) still gets one.
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

# Repo root, two levels up from buildsystems/cmake/.
get_filename_component(LAB_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../.." ABSOLUTE)

# ---- math -------------------------------------------------------------
# Mirrors BUILD.bazel's `math` cc_binary(linkshared = True): same
# src/math.cc, same public header include/abicheck_lab/math.h.
add_library(math SHARED "${LAB_ROOT}/src/math.cc")
target_include_directories(math PUBLIC "${LAB_ROOT}/include")
set_target_properties(math PROPERTIES
    OUTPUT_NAME "math"
    SOVERSION 1
    VERSION 1.0.0
)

# ---- strings ------------------------------------------------------------
# Mirrors strings_lib/BUILD.bazel's `strings` cc_binary(linkshared = True).
add_library(strings SHARED "${LAB_ROOT}/strings_lib/src/strings.cc")
target_include_directories(strings PUBLIC "${LAB_ROOT}/strings_lib/include")
set_target_properties(strings PROPERTIES
    OUTPUT_NAME "strings"
    SOVERSION 1
    VERSION 1.0.0
)

# ---- consumer_app ---------------------------------------------------------
# Mirrors consumer/BUILD.bazel's `consumer_app` cc_binary: links dynamically
# against `math` (never recompiles math.cc into the binary itself), same as
# consumer/BUILD.bazel's cc_import wrapping.
add_executable(consumer_app "${LAB_ROOT}/consumer/app.cc")
target_include_directories(consumer_app PRIVATE "${LAB_ROOT}/include")
target_link_libraries(consumer_app PRIVATE math)

# Smoke-testable target list this profile's backend/driver expects to find
# under the build directory: libmath.so, libstrings.so, consumer_app.
