#include "abicheck_lab/math.h"

// No functional/ABI change -- this comment exists to put this file in
// `depth: source`'s `scope=changed` replay for this PR, so the coverage
// contract's export-to-source ratio check has real translation units to
// verify against instead of being unenforceable for this diff.
// Fresh-main control rerun: abicheck/abicheck@84cf3d4 (PR #788 merged --
// export-accounting double-count fix, public-header-dir dump/scan parity,
// scan --build-target) against lab main d3f99d2.
namespace abicheck_lab {

int Calculator::add(int left, int right) const { return left + right; }
int Calculator::multiply(int left, int right) const { return left * right; }
int api_version() { return 1; }

}  // namespace abicheck_lab
