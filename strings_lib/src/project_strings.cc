#include "abicheck_lab/strings.h"
#include "abicheck_lab/core_c_api.h"

namespace abicheck_lab {
namespace {

int c_string_length(const char* s) {
  int len = 0;
  while (s != nullptr && s[len] != '\0') {
    ++len;
  }
  return len;
}

}  // namespace

Joiner::Joiner() {}

int Joiner::total_length(const char* a, const char* b) const {
  return c_string_length(a) + c_string_length(b);
}

int strings_api_version() { return abicheck_lab_core_scale(1) / 2; }

}  // namespace abicheck_lab
