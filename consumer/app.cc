// A tiny "real application" consumer of //:math (architecture review P1-6:
// consumer/app-scoped validation). Deliberately calls only
// Calculator::add(), never Calculator::multiply() or api_version() -- so
// this binary's own actual dynamic-symbol imports scope out any change to
// the functions it never calls, exercising abicheck's `compare --used-by`
// (ADR: app-scoped comparison folds appcompat's actual-import scoping into
// the primary verdict) against a real, separately-built consumer binary
// rather than a synthetic import list.
#include "abicheck_lab/math.h"

#include <cstdio>

int main() {
  abicheck_lab::Calculator calc;
  int result = calc.add(2, 3);
  std::printf("%d\n", result);
  return 0;
}
