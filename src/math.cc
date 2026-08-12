#include "abicheck_lab/math.h"

namespace abicheck_lab {

int Calculator::add(int left, int right) const { return left + right; }
int Calculator::multiply(int left, int right) const { return left * right; }
int api_version() { return 1; }

}  // namespace abicheck_lab
