#pragma once
/* What the servo module needs from main.c's link layer. */
#include <stddef.h>

/* Queue one text line (already newline-terminated) for the Pi link and the USB console.
 * Never blocks: if the queue is full the line is dropped. */
void tubi_link_send_text(const char *text, size_t n);

/* Print the `tasks` diagnostic (minimum free stack of every task, free heap). */
void tubi_report_tasks(void);
