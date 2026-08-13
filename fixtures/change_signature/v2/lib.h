#pragma once

namespace scenario {

// Parameter type changed from int (v1) to long (v2) -- same name,
// different mangled symbol.
int transform(long x);

}  // namespace scenario
