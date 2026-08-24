#include "abicheck_lab/core.h"
#include "abicheck_lab/core_c_api.h"

namespace abicheck_lab {

int core_api_version() { return 1; }

}  // namespace abicheck_lab

extern "C" int abicheck_lab_core_scale(int value) { return value * 2; }
