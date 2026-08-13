#pragma once

namespace scenario {

int base_function(int x);

// Added in v2. Purely additive: base_function's ABI is untouched, and a
// caller built against v1 keeps working against v2 -- it just can't see
// this new symbol until it's rebuilt/relinked against it.
int new_function(int x);

}  // namespace scenario
