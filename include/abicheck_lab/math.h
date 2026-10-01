#pragma once

namespace abicheck_lab {

// Stable public ABI used by the GitHub Actions test matrix.
class Calculator {
 public:
  Calculator() = default;
  int add(int left, int right) const;
  int multiply(int left, int right) const;
};

int api_version();

// Header-only convenience API: inline by design, so it introduces no
// exported symbol and must remain ABI-compatible with existing consumers.
inline bool is_nonnegative(int value) { return value >= 0; }

}  // namespace abicheck_lab
