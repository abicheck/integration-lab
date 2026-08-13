#pragma once

namespace scenario {

// legacy_metric removed -- the ONE finding the suppression rule accepts.

// required_api's parameter type changed (int -> long) -- a NEW,
// unaccepted break. C++ mangling encodes parameter types, so this is the
// old mangled symbol disappearing plus a new one appearing.
int required_api(long x);

}  // namespace scenario
