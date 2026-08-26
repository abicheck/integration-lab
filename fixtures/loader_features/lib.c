/* One source, many emitted loader/runtime feature sets.
 *
 * Every case in `loader_scenarios` (scenarios/manifest.yaml) links THIS
 * file twice, changing only linker and codegen flags.  Nothing about the
 * C source differs between the two sides of any case, so a finding can
 * only come from the loader/runtime features the toolchain emitted -- not
 * from a source-level change that happened to ride along.
 *
 * The contents exist to give the linker something to emit those features
 * for:
 *   - `lab_tls_slot` is a thread-local, so -ftls-model changes whether
 *     DF_STATIC_TLS appears and whether __tls_get_addr is imported.
 *   - `g_table` holds relocatable pointers, so there are relative
 *     relocations for -z pack-relative-relocs to fold into DT_RELR.
 *   - two exported functions give a version script two nodes to place
 *     symbols in.
 */
#include <stddef.h>

__thread int lab_tls_slot;

static int *g_table[8] = {0};

int lab_entry(int v) {
  g_table[v & 7] = &lab_tls_slot;
  return lab_tls_slot + v;
}

int lab_other(int v) { return v * 2; }
