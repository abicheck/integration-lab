#pragma once

namespace scenario {

int kept_function(int x);

// Removed in v2. Any binary already linked against v1's .so and calling
// this symbol fails to resolve it against v2 -- a binary ABI break.
int removed_function(int x);

}  // namespace scenario
