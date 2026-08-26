#include "api.h"

static client_word g_last = 0;

void lab_consume(client_word value) { g_last = value; }

client_word lab_produce(void) { return g_last; }
