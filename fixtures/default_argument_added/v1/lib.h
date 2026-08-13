#pragma once

namespace scenario {

// v2 gives `step` a default value. The mangled symbol is unchanged (default
// arguments aren't part of Itanium mangling), so this is invisible at
// binary/DWARF depth -- but it's a real, header-visible source/API change:
// a caller compiled against v2's header can call scaled(5) with one
// argument, while a caller still compiled against v1's header cannot. This
// is exactly the class of change abicheck's `depth: source` L4 evidence
// (not L0/L1 binary-only depth) is needed to detect as API_BREAK -- see
// AGENTS.md's "Default argument added" P0 fix this scenario regression-
// tests.
int scaled(int value, int step);

}  // namespace scenario
