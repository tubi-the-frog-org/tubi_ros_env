/*
 * Tubi the Wizard Frog -- the four hobby servos (MCPWM, one 50 Hz timer) and their motion.
 * Board: DOIT ESP32 DEVKIT V1 (ESP32-D0WD-V3). Derived from motor_esp2/src/pwm.c; the servo and
 * state-machine logic is the same, but commands now arrive through servos_handle_line() instead of
 * a UART owned by this file, because on this board the Pi's single serial link also carries audio.
 *
 *   servo      GPIO   job
 *   HEAD       15     bobs up at the start of every spoken word, down at the end   (strapping pin)
 *   ARM        2      up/down gesture                                              (strapping pin)
 *   LAZY SUSAN 21     positional servo, 4 bins
 *   TRAP DOOR  19     closed / open
 *   Share GND between the servo supply and the ESP32.
 *
 *   GPIO2 and GPIO15 are boot-strapping pins. Servo signal inputs normally don't disturb them, but if
 *   flashing over USB ever fails ("Wrong boot mode"), unplug the ARM signal wire (GPIO2) and retry.
 *   (GPIO34-39 are input-only and cannot drive a servo; never use those.)
 *
 * One timer feeds two operators; each operator has two comparator/generator pairs -> 4 independent
 * pulse widths from one MCPWM group. All four servos are slewed toward their targets every 20 ms, so
 * they move at the same time, each at its own speed.
 *
 * ---- Text protocol: one command per line, '\n' ends it ---
 * Arrives on the Pi link (UART2, 921600, shared with the audio stream: see main.c) AND on the USB
 * console (UART0, 115200), so it also works from a plain serial monitor (pio device monitor).
 *
 *   Any mode:
 *     status                  print mode, state and every servo's angle/pulse
 *     test | run              switch mode (boots in RUN)
 *     bin <1-4>               lazy susan to bin 1..4          (alias: slot)
 *     door open | close       trap door
 *     head up | down          head to its calibrated pose
 *     arm  up | down          arm  to its calibrated pose
 *     talk <ms> <ms> ...      one entry per word: that word's duration in ms.
 *                             Head AND arm go up when each word starts and dip
 *                             back down before it ends. After the last word
 *                             both end in their rest (down) position. Runs by
 *                             itself -- no serial traffic between words, so no
 *                             timing jitter.
 *     dispense <1-4>          the final decision: lazy susan rotates to that
 *                             bin, then the trap door opens, stays open for
 *                             DOOR_HOLD_MS and closes. Runs by itself, at the
 *                             same time as a talk (separate servos). Replies
 *                             "OK dispense done" when finished.
 *     stop                    abort talk AND dispense; head/arm to rest, door closes
 *     tasks                   print each FreeRTOS task's minimum free stack + free heap (diagnostic)
 *
 *   TEST mode only (find out how far each servo should go):
 *     head <deg>   arm <deg>   lazy <deg>   door <deg>
 *     deg is 0..180, clamped to that servo's safe limits (see servos[]).
 *     Type e.g. "head 105", look at the linkage, repeat. Then copy the numbers
 *     into the POSE_* constants below.
 *     Lazy susan: find the angle of each funnel (lazy 20, lazy 55, ...), copy
 *     them into POSE_BIN1..4_DEG, then check with "bin 1".."bin 4".
 *     Trap door:  find the closed and open angles, copy them into
 *     POSE_DOOR_CLOSED / POSE_DOOR_OPEN, then check with "door open|close".
 */

#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>
#include <stdbool.h>
#include "driver/mcpwm_prelude.h"
#include "driver/gpio.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "esp_log.h"
#include "esp_system.h"
#include "servos.h"
#include "tubi_link.h"


static const char *PWMTAG = "SERVO";

#define PWM_RESOLUTION_HZ   1000000           /* 1 MHz -> 1 tick = 1 us */
#define PWM_PERIOD_TICKS    20000             /* 20000 us = 20 ms = 50 Hz */
#define CONTROL_PERIOD_MS   20                /* one update per PWM period */

/* ---- Calibrated poses (degrees) --------------------------------------- *
 * TUNE THESE in test mode. HEAD/ARM numbers are placeholders. The door and
 * lazy susan values are the old pulse widths converted at 500-2500 us = 0-180.
 */
#define POSE_HEAD_DOWN      150.0f            /* measured */
#define POSE_HEAD_UP        100.0f            /* measured */
#define POSE_ARM_DOWN       80.0f             /* measured: rest position */
#define POSE_ARM_UP         0.0f              /* measured: raised */
#define POSE_ARM_WORD_LOW   40.0f             /* arm dips to here between words, halfway down (TUNE) */
#define POSE_DOOR_CLOSED    117.0f            /* was 1800 us */
#define POSE_DOOR_OPEN      171.0f            /* was 2400 us */

#define NUM_BINS            4
#define POSE_BIN1_DEG       180.0f            /* was 2500 us */
#define POSE_BIN2_DEG       126.0f            /* was 1900 us */
#define POSE_BIN3_DEG       63.0f             /* was 1200 us */
#define POSE_BIN4_DEG       0.0f              /* was 400 us (below the 500 us clamp, so 0 deg) */
static const float POSE_BIN_DEG[NUM_BINS] = {
    POSE_BIN1_DEG, POSE_BIN2_DEG, POSE_BIN3_DEG, POSE_BIN4_DEG,
};

/* Dispense timing (ms). Servo "arrived" means the commanded position was
 * reached; real servos lag a little, so we wait a bit longer. */
#define LAZY_SETTLE_MS      300               /* after the lazy susan arrives, before the door opens */
#define DOOR_HOLD_MS        400               /* door stays open this long (snack drops) */
#define DOOR_SETTLE_MS      200               /* after the door closes, before reporting done */

/* While talking: head is up for this fraction of each word, then drops. */
#define TALK_UP_FRACTION    0.6f
#define TALK_MAX_WORDS      64

/* ---- Servo table ------------------------------------------------------ */

typedef enum { SERVO_HEAD, SERVO_ARM, SERVO_LAZY, SERVO_DOOR, SERVO_COUNT } servo_id_t;

typedef struct {
    const char *name;
    gpio_num_t gpio;
    uint32_t min_us, max_us;        /* pulse at 0 deg and at 180 deg */
    float min_deg, max_deg;         /* safe travel -- test-mode input is clamped to this */
    uint32_t step_us;               /* max pulse change per 20 ms tick (speed) */
    float home_deg;                 /* where it sits at boot */
    mcpwm_cmpr_handle_t cmpr;
    mcpwm_gen_handle_t gen;
    uint32_t current_us;
    uint32_t target_us;
} servo_t;

static servo_t servos[SERVO_COUNT] = {
    [SERVO_HEAD] = { .name = "head", .gpio = GPIO_NUM_15, .min_us = 500, .max_us = 2500,
                     .min_deg = 60,  .max_deg = 150, .step_us = 100, .home_deg = POSE_HEAD_DOWN },
    [SERVO_ARM]  = { .name = "arm",  .gpio = GPIO_NUM_2,  .min_us = 500, .max_us = 2500,
                     .min_deg = 0,   .max_deg = 180, .step_us = 120, .home_deg = POSE_ARM_DOWN },
    [SERVO_LAZY] = { .name = "lazy", .gpio = GPIO_NUM_21, .min_us = 500, .max_us = 2500,
                     .min_deg = 0,   .max_deg = 180, .step_us = 40,  .home_deg = POSE_BIN1_DEG },
    [SERVO_DOOR] = { .name = "door", .gpio = GPIO_NUM_19, .min_us = 500, .max_us = 2500,
                     .min_deg = 0,   .max_deg = 180, .step_us = 60,  .home_deg = POSE_DOOR_CLOSED },
};

/* ---- Shared state ------------------------------------------------------- *
 *
 * Two FreeRTOS tasks live in this file (started by servos_start):
 *
 *   servo_task  core 1  prio 6  every 20 ms: slew the four servos one step toward their targets.
 *                               The only code that touches MCPWM after init, and the most
 *                               timing-sensitive, so it gets the highest priority.
 *   anim_task   core 1  prio 5  every 20 ms: advance the talk and dispense state machines.
 *
 * Commands arrive from another task (main.c's link/usb tasks) through servos_handle_line().
 * The state machines and servo targets are shared, so every entry point takes state_lock (a
 * RECURSIVE mutex: servos_handle_line() holds it while calling move_*(), which take it again).
 * reply() never blocks (tubi_link_send_text only queues), so a slow UART write can't stall the
 * servo slewing or a state machine while the lock is held.
 */
#define TASK_PRIO_SERVO   6
#define TASK_PRIO_ANIM    5

static SemaphoreHandle_t state_lock;   /* guards servos[], mode, talk_*, dsp_* */
static TaskHandle_t      h_servo, h_anim;

#define LOCK()   xSemaphoreTakeRecursive(state_lock, portMAX_DELAY)
#define UNLOCK() xSemaphoreGiveRecursive(state_lock)

static uint32_t deg_to_us(const servo_t *s, float deg)
{
    return s->min_us + (uint32_t)((deg / 180.0f) * (float)(s->max_us - s->min_us) + 0.5f);
}

static float us_to_deg(const servo_t *s, uint32_t us)
{
    return ((float)us - (float)s->min_us) * 180.0f / (float)(s->max_us - s->min_us);
}

/* Set a new target angle (clamped to the servo's safe range). Returns the
 * angle actually used. The 20 ms loop slews the servo there. */
static float servo_set_angle(servo_id_t id, float deg)
{
    servo_t *s = &servos[id];
    if (deg < s->min_deg) deg = s->min_deg;
    if (deg > s->max_deg) deg = s->max_deg;
    s->target_us = deg_to_us(s, deg);
    return deg;
}

/* Move ONE slew step toward the target, called once per control period. */
static void servo_tick(servo_t *s)
{
    if (s->current_us == s->target_us) {
        return;
    }
    uint32_t next;
    if (s->current_us < s->target_us) {
        next = (s->target_us - s->current_us < s->step_us) ? s->target_us
                                                          : s->current_us + s->step_us;
    } else {
        next = (s->current_us - s->target_us < s->step_us) ? s->target_us
                                                          : s->current_us - s->step_us;
    }
    s->current_us = next;
    ESP_ERROR_CHECK(mcpwm_comparator_set_compare_value(s->cmpr, next));
}

/* ---- The four movement functions -------------------------------------- */

/* 1. head: any angle 0..180 (clamped). */
void move_head(float deg)            { LOCK(); servo_set_angle(SERVO_HEAD, deg); UNLOCK(); }

/* 2. arm: any angle 0..180 (clamped). */
void move_arm(float deg)             { LOCK(); servo_set_angle(SERVO_ARM, deg); UNLOCK(); }

/* 3. lazy susan: bin 1..4. Returns false for a bad bin number. */
bool move_lazy_susan(int bin)
{
    if (bin < 1 || bin > NUM_BINS) return false;
    LOCK();
    servo_set_angle(SERVO_LAZY, POSE_BIN_DEG[bin - 1]);
    UNLOCK();
    return true;
}

/* 4. trap door. */
void move_trap_door(bool open)
{
    LOCK();
    servo_set_angle(SERVO_DOOR, open ? POSE_DOOR_OPEN : POSE_DOOR_CLOSED);
    UNLOCK();
}

/* ---- MCPWM setup ------------------------------------------------------ */

static mcpwm_timer_handle_t timer = NULL;
static mcpwm_oper_handle_t opers[2] = { NULL, NULL };

static void pwm_init(void)
{
    mcpwm_timer_config_t timer_config = {
        .group_id      = 0,
        .clk_src       = MCPWM_TIMER_CLK_SRC_DEFAULT,
        .resolution_hz = PWM_RESOLUTION_HZ,
        .count_mode    = MCPWM_TIMER_COUNT_MODE_UP,
        .period_ticks  = PWM_PERIOD_TICKS,
        .intr_priority = 0,
    };
    ESP_ERROR_CHECK(mcpwm_new_timer(&timer_config, &timer));

    /* An operator has 2 comparators + 2 generators, so 2 operators = 4 servos. */
    mcpwm_operator_config_t oper_config = { .group_id = 0, .intr_priority = 0 };
    for (int i = 0; i < 2; i++) {
        ESP_ERROR_CHECK(mcpwm_new_operator(&oper_config, &opers[i]));
        ESP_ERROR_CHECK(mcpwm_operator_connect_timer(opers[i], timer));
    }

    for (int i = 0; i < SERVO_COUNT; i++) {
        servo_t *s = &servos[i];
        mcpwm_oper_handle_t oper = opers[i / 2];

        /* New compare values latch at the start of a period (no glitched pulses). */
        mcpwm_comparator_config_t cmpr_config = { .intr_priority = 0, .flags.update_cmp_on_tez = true };
        ESP_ERROR_CHECK(mcpwm_new_comparator(oper, &cmpr_config, &s->cmpr));

        s->current_us = deg_to_us(s, s->home_deg);
        s->target_us  = s->current_us;
        ESP_ERROR_CHECK(mcpwm_comparator_set_compare_value(s->cmpr, s->current_us));

        /* Input-only pins (GPIO34-39) can't drive a servo. Skip this one instead of
         * letting ESP_ERROR_CHECK abort the whole boot and take the other servos down. */
        if (!GPIO_IS_VALID_OUTPUT_GPIO(s->gpio)) {
            ESP_LOGE(PWMTAG, "%s: GPIO %d is input-only on the ESP32 and cannot output PWM; "
                             "this servo will NOT move. Rewire it to an output pin.",
                     s->name, s->gpio);
            continue;
        }

        mcpwm_generator_config_t gen_config = { .gen_gpio_num = s->gpio };
        ESP_ERROR_CHECK(mcpwm_new_generator(oper, &gen_config, &s->gen));

        /* Period starts (timer empty): HIGH. Timer hits the compare value: LOW. */
        ESP_ERROR_CHECK(mcpwm_generator_set_action_on_timer_event(
            s->gen,
            MCPWM_GEN_TIMER_EVENT_ACTION(MCPWM_TIMER_DIRECTION_UP,
                                         MCPWM_TIMER_EVENT_EMPTY,
                                         MCPWM_GEN_ACTION_HIGH)));
        ESP_ERROR_CHECK(mcpwm_generator_set_action_on_compare_event(
            s->gen,
            MCPWM_GEN_COMPARE_EVENT_ACTION(MCPWM_TIMER_DIRECTION_UP,
                                           s->cmpr,
                                           MCPWM_GEN_ACTION_LOW)));
        ESP_LOGI(PWMTAG, "%s on GPIO %d, home %.0f deg (%u us)",
                 s->name, s->gpio, s->home_deg, (unsigned)s->current_us);
    }

    ESP_ERROR_CHECK(mcpwm_timer_enable(timer));
    ESP_ERROR_CHECK(mcpwm_timer_start_stop(timer, MCPWM_TIMER_START_NO_STOP));
}

/* ---- State machine ---------------------------------------------------- *
 *
 *   mode:   MODE_RUN  (default) -- what the Pi drives
 *           MODE_TEST           -- adds raw per-servo angle commands
 *   Two independent state machines (advanced by anim_task every 20 ms), so head/arm
 *   and lazy susan/door can move at the same time:
 *
 *   talk:      TALK_IDLE -> TALK_ACTIVE -> TALK_IDLE
 *     per word: [word starts] head UP, arm UP --(TALK_UP_FRACTION)--> head
 *     DOWN, arm dips --(rest of word)--> next word starts. After the last
 *     word: head and arm to rest.
 *
 *   dispense:  DSP_IDLE -> DSP_ROTATING -> DSP_OPENING -> DSP_CLOSING -> DSP_IDLE
 *     ROTATING: door closed, lazy susan moves to the bin; wait until it has
 *               arrived + LAZY_SETTLE_MS.
 *     OPENING:  door opens; wait until open + DOOR_HOLD_MS.
 *     CLOSING:  door closes; wait until closed + DOOR_SETTLE_MS, then done.
 *
 *   `stop` aborts both.
 */

typedef enum { MODE_RUN, MODE_TEST } mode_t_;
typedef enum { TALK_IDLE, TALK_ACTIVE } talk_state_t;
typedef enum { DSP_IDLE, DSP_ROTATING, DSP_OPENING, DSP_CLOSING } dispense_state_t;

static mode_t_          mode        = MODE_RUN;
static talk_state_t     talk_state  = TALK_IDLE;
static dispense_state_t dsp_state   = DSP_IDLE;
static int32_t          dsp_arrived_ms = -1;   /* when the current step's servo reached its target */

static uint32_t talk_ms[TALK_MAX_WORDS];
static int      talk_count = 0;
static int      talk_idx = 0;
static bool     talk_head_up = false;
static int32_t  talk_next_event_ms = 0;     /* tick time of the next up/down flip */

static int32_t now_ms(void)
{
    return (int32_t)(xTaskGetTickCount() * portTICK_PERIOD_MS);
}

/* Never blocks and never touches a UART: formats the line and hands it to the link layer, which
 * queues it for the Pi and the USB console (a full queue just drops this reply). */
static void reply(const char *fmt, ...)
{
    char buf[160];
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(buf, sizeof(buf) - 2, fmt, ap);
    va_end(ap);
    if (n < 0) return;
    if (n > (int)sizeof(buf) - 3) n = sizeof(buf) - 3;
    buf[n++] = '\n';
    tubi_link_send_text(buf, (size_t)n);
}

/* End of a sentence (or `stop`): head and arm to rest. */
static void talk_finish(void)
{
    talk_state = TALK_IDLE;
    talk_head_up = false;
    move_head(POSE_HEAD_DOWN);
    move_arm(POSE_ARM_DOWN);
}

static void talk_begin_word(void)
{
    move_head(POSE_HEAD_UP);
    move_arm(POSE_ARM_UP);
    talk_head_up = true;
    talk_next_event_ms = now_ms() + (int32_t)(talk_ms[talk_idx] * TALK_UP_FRACTION);
}

static void talk_start(void)
{
    talk_state = TALK_ACTIVE;
    talk_idx = 0;
    talk_begin_word();
}

static void talk_tick(void)
{
    if (talk_state != TALK_ACTIVE) return;
    if ((int32_t)(now_ms() - talk_next_event_ms) < 0) return;

    if (talk_head_up) {
        /* Head drops at the end of the word; next word starts after the remainder. */
        move_head(POSE_HEAD_DOWN);
        /* Every word dips the arm; the last word goes straight to rest. */
        move_arm(talk_idx < talk_count - 1 ? POSE_ARM_WORD_LOW : POSE_ARM_DOWN);
        talk_head_up = false;
        talk_next_event_ms += (int32_t)(talk_ms[talk_idx] * (1.0f - TALK_UP_FRACTION));
    } else {
        talk_idx++;
        if (talk_idx >= talk_count) {
            talk_finish();
            reply("OK talk done");
        } else {
            talk_begin_word();
        }
    }
}

/* ---- Dispense state machine ------------------------------------------- */

static bool servo_at_target(servo_id_t id)
{
    return servos[id].current_us == servos[id].target_us;
}

/* True once the servo has been at its target for settle_ms. */
static bool servo_settled(servo_id_t id, uint32_t settle_ms)
{
    if (!servo_at_target(id)) {
        dsp_arrived_ms = -1;
        return false;
    }
    if (dsp_arrived_ms < 0) dsp_arrived_ms = now_ms();
    return (int32_t)(now_ms() - dsp_arrived_ms) >= (int32_t)settle_ms;
}

static void dispense_enter(dispense_state_t next)
{
    dsp_state = next;
    dsp_arrived_ms = -1;
    switch (next) {
    case DSP_OPENING: move_trap_door(true);  break;
    case DSP_CLOSING: move_trap_door(false); break;
    default: break;
    }
}

static void dispense_start(int bin)
{
    move_trap_door(false);          /* make sure it is shut while the tray turns */
    move_lazy_susan(bin);
    dispense_enter(DSP_ROTATING);
    dsp_arrived_ms = -1;
}

static void dispense_abort(void)
{
    dsp_state = DSP_IDLE;
    move_trap_door(false);
    servos[SERVO_LAZY].target_us = servos[SERVO_LAZY].current_us;   /* halt the tray where it is */
}

static void dispense_tick(void)
{
    switch (dsp_state) {
    case DSP_IDLE:
        break;
    case DSP_ROTATING:
        if (servo_settled(SERVO_LAZY, LAZY_SETTLE_MS)) dispense_enter(DSP_OPENING);
        break;
    case DSP_OPENING:
        if (servo_settled(SERVO_DOOR, DOOR_HOLD_MS)) dispense_enter(DSP_CLOSING);
        break;
    case DSP_CLOSING:
        if (servo_settled(SERVO_DOOR, DOOR_SETTLE_MS)) {
            dsp_state = DSP_IDLE;
            reply("OK dispense done");
        }
        break;
    }
}

/* ---- Command parser --------------------------------------------------- */

static bool parse_float(const char *tok, float *out)
{
    if (!tok) return false;
    char *end;
    float v = strtof(tok, &end);
    if (end == tok || *end != '\0') return false;
    *out = v;
    return true;
}

static void cmd_status(void)
{
    reply("mode=%s talk=%s dispense=%s", mode == MODE_TEST ? "test" : "run",
          talk_state == TALK_ACTIVE ? "active" : "idle",
          dsp_state == DSP_IDLE ? "idle" : dsp_state == DSP_ROTATING ? "rotating"
                                : dsp_state == DSP_OPENING ? "opening" : "closing");
    for (int i = 0; i < SERVO_COUNT; i++) {
        reply("  %-4s %6.1f deg  %4u us (target %4u us)  limits %.0f-%.0f deg",
              servos[i].name, us_to_deg(&servos[i], servos[i].current_us),
              (unsigned)servos[i].current_us, (unsigned)servos[i].target_us,
              servos[i].min_deg, servos[i].max_deg);
    }
}

/* head/arm/lazy/door: a keyword anywhere, a raw angle only in test mode. */
static void cmd_servo(servo_id_t id, const char *arg)
{
    if (!arg) { reply("ERR %s needs an argument", servos[id].name); return; }

    if (id == SERVO_HEAD && !strcmp(arg, "up"))    { move_head(POSE_HEAD_UP);   reply("OK head up"); return; }
    if (id == SERVO_HEAD && !strcmp(arg, "down"))  { move_head(POSE_HEAD_DOWN); reply("OK head down"); return; }
    if (id == SERVO_ARM  && !strcmp(arg, "up"))    { move_arm(POSE_ARM_UP);     reply("OK arm up"); return; }
    if (id == SERVO_ARM  && !strcmp(arg, "down"))  { move_arm(POSE_ARM_DOWN);   reply("OK arm down"); return; }
    if (id == SERVO_DOOR && !strcmp(arg, "open"))  { move_trap_door(true);      reply("OK door open"); return; }
    if (id == SERVO_DOOR && !strcmp(arg, "close")) { move_trap_door(false);     reply("OK door close"); return; }

    float deg;
    if (!parse_float(arg, &deg)) { reply("ERR bad argument '%s'", arg); return; }
    if (mode != MODE_TEST)       { reply("ERR raw angles need test mode (send 'test')"); return; }
    float used = servo_set_angle(id, deg);
    if (used != deg) reply("OK %s -> %.1f deg (clamped from %.1f)", servos[id].name, used, deg);
    else             reply("OK %s -> %.1f deg", servos[id].name, used);
}

static void handle_line(char *line)
{
    for (char *p = line; *p; p++) *p = (char)tolower((unsigned char)*p);

    char *save = NULL;
    char *cmd = strtok_r(line, " \t", &save);
    if (!cmd) return;

    if (!strcmp(cmd, "status")) { cmd_status(); return; }

    if (!strcmp(cmd, "test")) { mode = MODE_TEST; reply("OK mode=test"); return; }
    if (!strcmp(cmd, "run"))  { mode = MODE_RUN;  reply("OK mode=run");  return; }

    if (!strcmp(cmd, "stop")) { talk_finish(); dispense_abort(); reply("OK stopped"); return; }

    if (!strcmp(cmd, "dispense")) {
        char *arg = strtok_r(NULL, " \t", &save);
        int bin = arg ? atoi(arg) : 0;
        if (bin < 1 || bin > NUM_BINS)  { reply("ERR dispense needs a bin 1..%d", NUM_BINS); return; }
        if (dsp_state != DSP_IDLE)      { reply("ERR dispense busy"); return; }
        dispense_start(bin);
        reply("OK dispense bin %d", bin);
        return;
    }

    if (!strcmp(cmd, "bin") || !strcmp(cmd, "slot")) {
        char *arg = strtok_r(NULL, " \t", &save);
        int bin = arg ? atoi(arg) : 0;
        if (move_lazy_susan(bin)) reply("OK bin %d", bin);
        else                      reply("ERR bin must be 1..%d", NUM_BINS);
        return;
    }

    if (!strcmp(cmd, "talk")) {
        int n = 0;
        char *tok;
        while (n < TALK_MAX_WORDS && (tok = strtok_r(NULL, " \t", &save)) != NULL) {
            float ms;
            if (!parse_float(tok, &ms) || ms < 50 || ms > 5000) { reply("ERR bad word duration '%s'", tok); return; }
            talk_ms[n++] = (uint32_t)ms;
        }
        if (n == 0) { reply("ERR talk needs word durations in ms"); return; }
        talk_count = n;
        talk_start();
        reply("OK talk %d words", n);
        return;
    }

    char *arg = strtok_r(NULL, " \t", &save);
    if (!strcmp(cmd, "head")) { cmd_servo(SERVO_HEAD, arg); return; }
    if (!strcmp(cmd, "arm"))  { cmd_servo(SERVO_ARM, arg);  return; }
    if (!strcmp(cmd, "lazy")) { cmd_servo(SERVO_LAZY, arg); return; }
    if (!strcmp(cmd, "door")) { cmd_servo(SERVO_DOOR, arg); return; }

    if (!strcmp(cmd, "tasks")) { tubi_report_tasks(); return; }

    reply("ERR unknown command '%s'", cmd);
}

/* ---- Tasks and public API --------------------------------------------- */

/* Slews all four servos one step every control period. */
static void servo_task(void *arg)
{
    TickType_t last_wake = xTaskGetTickCount();
    for (;;) {
        vTaskDelayUntil(&last_wake, pdMS_TO_TICKS(CONTROL_PERIOD_MS));
        LOCK();
        for (int i = 0; i < SERVO_COUNT; i++) {
            servo_tick(&servos[i]);
        }
        UNLOCK();
    }
}

/* Advances the talk and dispense state machines every control period. */
static void anim_task(void *arg)
{
    TickType_t last_wake = xTaskGetTickCount();
    for (;;) {
        vTaskDelayUntil(&last_wake, pdMS_TO_TICKS(CONTROL_PERIOD_MS));
        LOCK();
        talk_tick();
        dispense_tick();
        UNLOCK();
    }
}

void servos_start(void)
{
    state_lock = xSemaphoreCreateRecursiveMutex();
    configASSERT(state_lock != NULL);

    pwm_init();

    if (xTaskCreatePinnedToCore(servo_task, "servo", 3072, NULL, TASK_PRIO_SERVO, &h_servo, 1) != pdPASS ||
        xTaskCreatePinnedToCore(anim_task,  "anim",  3072, NULL, TASK_PRIO_ANIM,  &h_anim,  1) != pdPASS) {
        ESP_LOGE(PWMTAG, "could not create the servo tasks");
        abort();  /* a missing task means a dead servo loop: fail loudly at boot */
    }
}

/* One text command line (modified in place). Safe to call from any task. */
void servos_handle_line(char *line)
{
    LOCK();
    handle_line(line);
    UNLOCK();
}

void servos_stack_free(unsigned *servo_task_free, unsigned *anim_task_free)
{
    *servo_task_free = (unsigned)uxTaskGetStackHighWaterMark(h_servo);
    *anim_task_free  = (unsigned)uxTaskGetStackHighWaterMark(h_anim);
}
