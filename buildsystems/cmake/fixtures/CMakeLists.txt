# PR3 item 2 (design doc section 18: generalized scenario runner). A
# single, generic CMake project parameterized by -DFIXTURE_DIR=<path to a
# fixtures/<name>/{v1,v2}/ directory> -- NOT one CMakeLists.txt per
# fixture. Every scenario fixture under fixtures/ follows the identical
# shape (fixtures/<name>/BUILD.bazel's own comment convention: one
# lib.cc + lib.h, one `impl` target), so one parameterized project covers
# every fixture scripts/run_scenario.py's --build-system cmake path
# declares in scenarios/build-matrix.yaml, without hand-writing N nearly
# identical CMakeLists.txt files.
#
# Configured/built fresh per fixture VERSION directory by
# scripts/run_scenario.py's run_cmake_build() -- a `cmake -S ... -B
# <scratch-dir> -DFIXTURE_DIR=fixtures/add_function/v1 && cmake --build
# <scratch-dir>` pair per side, same as ci/backends/cmake.py's own
# configure()/build() pattern for the main library, just pointed at a
# fixture directory instead of src/.
cmake_minimum_required(VERSION 3.20)
project(abicheck_lab_fixture CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

if(NOT DEFINED FIXTURE_DIR)
  message(FATAL_ERROR
    "FIXTURE_DIR must be set: cmake -S buildsystems/cmake/fixtures -B <build-dir> -DFIXTURE_DIR=<path to a fixtures/<name>/{v1,v2} directory>")
endif()

get_filename_component(FIXTURE_DIR_ABS "${FIXTURE_DIR}" ABSOLUTE)

if(NOT EXISTS "${FIXTURE_DIR_ABS}/lib.cc")
  message(FATAL_ERROR "FIXTURE_DIR=${FIXTURE_DIR_ABS} has no lib.cc (expected the fixtures/<name>/{v1,v2} shape)")
endif()

# Every fixture's own BUILD.bazel names its shared object target "impl"
# (bazel-bin/fixtures/<name>/<version>/libimpl.so) -- matched here so
# scripts/run_scenario.py's cmake path stages the same libimpl.so
# filename its bazel path already expects.
add_library(impl SHARED "${FIXTURE_DIR_ABS}/lib.cc")
target_include_directories(impl PRIVATE "${FIXTURE_DIR_ABS}")
set_target_properties(impl PROPERTIES OUTPUT_NAME "impl")
