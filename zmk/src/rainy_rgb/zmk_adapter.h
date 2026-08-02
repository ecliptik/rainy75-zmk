#ifndef RAINY_RGB_ZMK_ADAPTER_H
#define RAINY_RGB_ZMK_ADAPTER_H
#include <stdint.h>
#include <stdbool.h>
#include "color.h"
int rrgb_strip_init(void);
void rrgb_strip_show(const struct rrgb *px, uint16_t n);

/* Drive the LED VCC rail (PC2). Powering on re-asserts the full drive
 * configuration, so it doubles as the recovery for a pin found misconfigured. */
void rrgb_strip_power(bool on);

/* Sample what the rail pin actually reads, for the divergence trap. */
#define RRGB_RAIL_HIGH      BIT(0)   /* PC2 output data high (rail powered) */
#define RRGB_RAIL_OUT_EN    BIT(1)   /* PC2 output driver enabled */
#define RRGB_RAIL_GPIO_MODE BIT(2)   /* PC2 in GPIO mode (not a peripheral) */
uint8_t rrgb_strip_rail_state(void);
#endif
