#include "abicheck_lab/math.h"
#include "abicheck_lab/core_c_api.h"

namespace abicheck_lab {

int Calculator::add(int left, int right) const { return left + right; }
int Calculator::multiply(int left, int right) const { return left * right; }
int api_version() { return abicheck_lab_core_scale(1) / 2; }

}  // namespace abicheck_lab
