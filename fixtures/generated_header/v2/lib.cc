// See v1/lib.cc's own comment on why this is a full package-relative
// include path, not a bare filename.
#include "fixtures/generated_header/v2/lib.h"

namespace scenario {

int kept_function(int x) { return x + 1; }

}  // namespace scenario
