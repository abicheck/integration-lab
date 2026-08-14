// A generated header (see gen_header.py's own docstring) can only be
// resolved via `-iquote bazel-out/.../bin`, since gcc's own
// same-directory-as-source quote-include check only ever succeeds for a
// real, checked-in source-tree file -- so the include text itself must
// carry the full package-relative path, not a bare filename (verified
// empirically: a bare `#include "lib.h"` fails with "No such file or
// directory" even though the generated file exists and Bazel's own
// CcInfo.compilation_context.headers correctly lists it).
#include "fixtures/generated_header/v1/lib.h"

namespace scenario {

int kept_function(int x) { return x + 1; }
int removed_function(int x) { return x * 2; }

}  // namespace scenario
