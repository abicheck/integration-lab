#pragma once

namespace scenario {

// v2 gives `step` a default value. The mangled symbol is unchanged (default
// arguments aren't part of Itanium mangling), and abicheck deliberately
// never flags gaining a default at all (see abicheck/checker_policy.py's
// ChangeKind docstrings): it's purely additive -- an existing caller that
// already passes both arguments is completely unaffected; only new code
// gets the option to omit the second one. See scenarios/manifest.yaml for
// this scenario's full rationale (expected_verdict: COMPATIBLE).
int scaled(int value, int step);

}  // namespace scenario
