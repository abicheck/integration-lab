#pragma once

namespace abicheck_lab {

// Stable public ABI used by the GitHub Actions test matrix.
class Calculator {
 public:
  Calculator() = default;
  int add(int left, int right) const;
  int multiply(int left, int right) const;
  int subtract(int left, int right) const;
};

int api_version();

}  // namespace abicheck_lab
