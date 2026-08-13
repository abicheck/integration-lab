#pragma once

namespace scenario {

// v2 removes this one -- the ONE finding the suppression rule accepts.
int legacy_metric(int x);

// v2 changes this parameter's type -- a NEW, unaccepted break the
// suppression rule (scoped only to legacy_metric) must NOT silence.
int required_api(int x);

}  // namespace scenario
