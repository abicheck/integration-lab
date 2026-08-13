#pragma once

namespace scenario {

// `step` now has a default value. Same mangled symbol as v1 -- a source/API
// change, not a binary one.
int scaled(int value, int step = 2);

}  // namespace scenario
