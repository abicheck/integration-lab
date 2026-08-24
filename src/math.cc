#include "abicheck_lab/math.h"
#include "abicheck_lab/core_c_api.h"

// No functional/ABI change -- this comment exists to put this file in
// `depth: source`'s `scope=changed` replay for this PR, so the coverage
// contract's export-to-source ratio check has real translation units to
// verify against instead of being unenforceable for this diff.
namespace abicheck_lab {

int Calculator::add(int left, int right) const { return left + right; }
int Calculator::multiply(int left, int right) const { return left * right; }
int api_version() { return abicheck_lab_core_scale(1) / 2; }

}  // namespace abicheck_lab
