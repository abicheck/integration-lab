#pragma once

namespace scenario {

// v2 adds a second, unrelated function alongside this one -- purely
// additive, existing callers of base_function are unaffected.
int base_function(int x);

}  // namespace scenario
