#pragma once

namespace scenario {

// v2 changes this parameter's type. C++ name mangling encodes parameter
// types in the symbol name, so this isn't an in-place signature edit at
// the ABI level -- it's the old mangled symbol disappearing and a new one
// (for the new signature) appearing. Breaking for any caller compiled
// against v1's signature.
int transform(int x);

}  // namespace scenario
