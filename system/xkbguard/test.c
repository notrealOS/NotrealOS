#include <stdio.h>
#include <stdint.h>
struct xkb_state;
int xkb_state_update_mask(struct xkb_state *, uint32_t, uint32_t, uint32_t, uint32_t, uint32_t, uint32_t);
int main(void) { printf("null state -> %d (no crash)\n", xkb_state_update_mask(NULL, 1, 0, 0, 0, 0, 0)); return 0; }
