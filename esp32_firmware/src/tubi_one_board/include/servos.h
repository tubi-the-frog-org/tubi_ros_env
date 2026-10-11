#pragma once
#include <stdbool.h>

/* Set up MCPWM and the four servo outputs, then start servo_task and anim_task. Call once. */
void servos_start(void);

/* Run one text command line (see the protocol in servos.c). The line is modified in place.
 * Thread-safe: call it from any task. */
void servos_handle_line(char *line);

/* Minimum stack ever free in the two servo tasks, in bytes (for the `tasks` diagnostic). */
void servos_stack_free(unsigned *servo_task_free, unsigned *anim_task_free);

/* The four movement functions. They set a target (and take the state lock themselves, so they are
 * safe to call from any task); servo_task slews all servos toward their targets every 20 ms. */
void move_head(float deg);          /* 0..180, clamped to the head's safe range */
void move_arm(float deg);           /* 0..180, clamped to the arm's safe range */
bool move_lazy_susan(int bin);      /* bin 1..4; false if out of range */
void move_trap_door(bool open);
