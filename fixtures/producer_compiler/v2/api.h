#ifndef LAB_PRODUCER_COMPILER_API_H
#define LAB_PRODUCER_COMPILER_API_H

// The public width of client_word is chosen by the PRODUCER compiler, not
// by the build system. Clang and GCC therefore ship genuinely different
// ABIs from one identical source tree -- which is the point of this
// fixture: a change confined to the non-Clang branch must be reported as
// breaking for the GCC-produced profile and as no change at all for the
// Clang-produced one.
#ifdef __clang__
using client_word = long;
#else
using client_word = long long;
#endif

// Mangles the parameter type into the exported symbol, so a client_word
// width change is visible at binary depth (no headers required) as a
// removed/added symbol pair rather than only in DWARF.
void lab_consume(client_word value);
client_word lab_produce(void);

#endif
