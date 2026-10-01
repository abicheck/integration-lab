#pragma once

namespace abicheck_lab {

// Stable public ABI used by the GitHub Actions test matrix.
class Calculator {
 public:
  Calculator() = default;
  int add(int left, int right) const;
  long multiply(long left, long right) const;
};

int api_version();

}  // namespace abicheck_lab
