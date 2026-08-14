#pragma once

namespace abicheck_lab {

// A second, independent library (architecture review P1-5: multi-library
// aggregate validation) -- deliberately unrelated to :math, so a change to
// one never touches the other's ABI. Lives in its OWN Bazel package
// (strings_lib/, its own BUILD.bazel) rather than the root one alongside
// :math/:math_api: `bazel query 'buildfiles(deps(//:math))'`
// (scripts/check_coverage_contract.py's own exemption mechanism) answers
// per FILE, not per target within a file -- if :strings' declaration lived
// in root BUILD.bazel too, any PR touching only :strings would still
// (correctly, if uselessly) disqualify :math's own coverage-contract
// exemption, since Bazel can't tell "this diff to BUILD.bazel only
// touched an unrelated target" without literal diff-content parsing. A
// separate package sidesteps the whole question: root BUILD.bazel simply
// never changes for a :strings-only PR. See README.md's "Multi-library
// aggregate gate" section.
//
// Public API deliberately avoids std::string/STL containers -- verified
// empirically: a direct-clang dump of a header taking std::string pulled
// in a >10MB L5 source-graph closure over libstdc++'s own template
// internals (visible even at the default `headers` depth, independent of
// the snapshot's own top-level type/function scoping), which would bloat
// this committed baseline for no reason connected to what this fixture
// actually validates. Plain C-shaped signatures keep the baseline small.
class Joiner {
 public:
  Joiner();
  int total_length(const char* a, const char* b) const;
};

int strings_api_version();

}  // namespace abicheck_lab
