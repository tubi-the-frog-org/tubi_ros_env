#ifndef PWM_H
#define PWM_H

#include <stdbool.h>

/* Set up the MCPWM timer/operators and the four servo outputs. */
void pwm_init(void);

/* The four movement functions. They set a target; the 20 ms control loop in
 * app_main() slews all servos toward their targets at the same time. */
void move_head(float deg);          /* 0..180, clamped to the head's safe range */
void move_arm(float deg);           /* 0..180, clamped to the arm's safe range */
bool move_lazy_susan(int bin);      /* bin 1..4; false if out of range */
void move_trap_door(bool open);

#endif
