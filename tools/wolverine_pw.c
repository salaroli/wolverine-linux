// wolverine_pw.c — native PipeWire bridge for the Razer Wolverine headset.
//
// Creates two virtual nodes in the PipeWire graph:
//   * "Wolverine Headphones"  — an Audio/Sink  (system plays into it; we read
//     the PCM out and the Python driver sends it to the controller's EP3 OUT).
//   * "Wolverine Microphone"  — an Audio/Source (we push mic PCM in from EP3 IN;
//     the system captures it).
//
// The C side owns the PipeWire thread loop and two lock-protected ring buffers.
// Python (ctypes) moves PCM between the rings and USB:
//   wpw_read_playback()  — pull audio the system wants to play  (sink  -> USB)
//   wpw_write_capture()  — push mic audio captured from the mic  (USB  -> source)
//
// This lives in C (not ctypes) because the SPA audio format helper
// spa_format_audio_raw_build() is a static-inline header function that cannot be
// reached from pure ctypes/cffi — it must be compiled in.
//
// Build:  make -C tools           (see tools/Makefile)
// Format: S16_LE, 48 kHz, configurable channel count (2).

#include <pipewire/pipewire.h>
#include <spa/param/audio/format-utils.h>
#include <spa/pod/builder.h>

#include <pthread.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

// ---------------------------------------------------------------------------
// Ring buffer (single lock, drop-oldest on overflow)
// ---------------------------------------------------------------------------

#define RING_CAP  (1u << 19)          // 512 KiB, power of two
#define RING_MASK (RING_CAP - 1u)

typedef struct {
    uint8_t *buf;
    uint32_t head;                    // write cursor
    uint32_t tail;                    // read cursor
    pthread_mutex_t lock;
} ring_t;

static void ring_init(ring_t *r) {
    r->buf = (uint8_t *)malloc(RING_CAP);
    r->head = r->tail = 0;
    pthread_mutex_init(&r->lock, NULL);
}

static void ring_free(ring_t *r) {
    free(r->buf);
    r->buf = NULL;
    pthread_mutex_destroy(&r->lock);
}

static void ring_write(ring_t *r, const uint8_t *src, uint32_t n) {
    if (n > RING_CAP) { src += (n - RING_CAP); n = RING_CAP; }
    pthread_mutex_lock(&r->lock);
    uint32_t used  = r->head - r->tail;
    uint32_t space = RING_CAP - used;
    if (n > space)                     // drop oldest to make room
        r->tail += (n - space);
    for (uint32_t i = 0; i < n; i++)
        r->buf[(r->head + i) & RING_MASK] = src[i];
    r->head += n;
    pthread_mutex_unlock(&r->lock);
}

static uint32_t ring_read(ring_t *r, uint8_t *dst, uint32_t n) {
    pthread_mutex_lock(&r->lock);
    uint32_t avail = r->head - r->tail;
    if (n > avail) n = avail;
    for (uint32_t i = 0; i < n; i++)
        dst[i] = r->buf[(r->tail + i) & RING_MASK];
    r->tail += n;
    pthread_mutex_unlock(&r->lock);
    return n;
}

// ---------------------------------------------------------------------------
// PipeWire state
// ---------------------------------------------------------------------------

static struct {
    struct pw_thread_loop *loop;
    struct pw_context     *context;
    struct pw_core        *core;
    struct pw_stream      *playback;   // Audio/Sink   (system -> us)
    struct pw_stream      *capture;    // Audio/Source (us -> system)
    struct spa_hook        playback_listener;
    struct spa_hook        capture_listener;
    uint32_t out_stride;               // bytes per frame, sink side
    uint32_t in_stride;                // bytes per frame, source side
    ring_t   play_ring;                // system playback audio, drained to USB
    ring_t   cap_ring;                 // mic audio from USB, served to system
    int      running;
} g;

// Sink: PipeWire hands us buffers full of the audio the system is playing.
static void on_process_playback(void *userdata) {
    (void)userdata;
    struct pw_buffer *b = pw_stream_dequeue_buffer(g.playback);
    if (!b) return;
    struct spa_data *d = &b->buffer->datas[0];
    if (d->data && d->chunk) {
        uint32_t size = d->chunk->size;
        uint32_t off  = d->chunk->offset;
        if (size)
            ring_write(&g.play_ring, (uint8_t *)d->data + off, size);
    }
    pw_stream_queue_buffer(g.playback, b);
}

// Source: PipeWire asks us to fill buffers with mic audio.
static void on_process_capture(void *userdata) {
    (void)userdata;
    struct pw_buffer *b = pw_stream_dequeue_buffer(g.capture);
    if (!b) return;
    struct spa_data *d = &b->buffer->datas[0];
    if (!d->data) { pw_stream_queue_buffer(g.capture, b); return; }

    uint32_t maxframes = d->maxsize / g.in_stride;
    uint32_t nframes   = b->requested ? (uint32_t)b->requested : maxframes;
    if (nframes > maxframes) nframes = maxframes;
    uint32_t nbytes = nframes * g.in_stride;

    uint32_t got = ring_read(&g.cap_ring, (uint8_t *)d->data, nbytes);
    if (got < nbytes)                                  // underrun -> silence
        memset((uint8_t *)d->data + got, 0, nbytes - got);

    d->chunk->offset = 0;
    d->chunk->stride = (int32_t)g.in_stride;
    d->chunk->size   = nbytes;
    pw_stream_queue_buffer(g.capture, b);
}

static const struct pw_stream_events playback_events = {
    PW_VERSION_STREAM_EVENTS,
    .process = on_process_playback,
};

static const struct pw_stream_events capture_events = {
    PW_VERSION_STREAM_EVENTS,
    .process = on_process_capture,
};

static const struct spa_pod *build_format(struct spa_pod_builder *b,
                                          int rate, int channels) {
    struct spa_audio_info_raw info;
    spa_zero(info);
    info.format   = SPA_AUDIO_FORMAT_S16_LE;
    info.rate     = (uint32_t)rate;
    info.channels = (uint32_t)channels;
    return spa_format_audio_raw_build(b, SPA_PARAM_EnumFormat, &info);
}

// ---------------------------------------------------------------------------
// Public API (called from Python via ctypes)
// ---------------------------------------------------------------------------

// Returns 0 on success, negative on failure. The sink (headphones) and source
// (mic) can use different rates/channel counts — the Wolverine's mic is not the
// same format as its output.
int wpw_start(int out_rate, int out_channels, int in_rate, int in_channels) {
    if (g.running) return 0;
    memset(&g, 0, sizeof(g));
    g.out_stride = (uint32_t)out_channels * sizeof(int16_t);
    g.in_stride  = (uint32_t)in_channels  * sizeof(int16_t);
    ring_init(&g.play_ring);
    ring_init(&g.cap_ring);

    pw_init(NULL, NULL);

    g.loop = pw_thread_loop_new("wolverine-audio", NULL);
    if (!g.loop) return -1;

    g.context = pw_context_new(pw_thread_loop_get_loop(g.loop), NULL, 0);
    if (!g.context) return -2;

    if (pw_thread_loop_start(g.loop) != 0) return -3;

    pw_thread_loop_lock(g.loop);

    g.core = pw_context_connect(g.context, NULL, 0);
    if (!g.core) { pw_thread_loop_unlock(g.loop); return -4; }

    uint8_t buffer[1024];
    struct spa_pod_builder pb;
    const struct spa_pod *params[1];

    // --- virtual sink: "Wolverine Headphones" ---
    g.playback = pw_stream_new_simple(
        pw_thread_loop_get_loop(g.loop),
        "Wolverine Headphones",
        pw_properties_new(
            PW_KEY_MEDIA_TYPE,        "Audio",
            PW_KEY_MEDIA_CLASS,       "Audio/Sink",
            PW_KEY_NODE_NAME,         "wolverine_headphones",
            PW_KEY_NODE_DESCRIPTION,  "Wolverine Headphones",
            NULL),
        &playback_events, NULL);
    pb = SPA_POD_BUILDER_INIT(buffer, sizeof(buffer));
    params[0] = build_format(&pb, out_rate, out_channels);
    pw_stream_connect(g.playback, PW_DIRECTION_INPUT, PW_ID_ANY,
        PW_STREAM_FLAG_AUTOCONNECT | PW_STREAM_FLAG_MAP_BUFFERS |
        PW_STREAM_FLAG_RT_PROCESS, params, 1);

    // --- virtual source: "Wolverine Microphone" ---
    g.capture = pw_stream_new_simple(
        pw_thread_loop_get_loop(g.loop),
        "Wolverine Microphone",
        pw_properties_new(
            PW_KEY_MEDIA_TYPE,        "Audio",
            PW_KEY_MEDIA_CLASS,       "Audio/Source",
            PW_KEY_NODE_NAME,         "wolverine_mic",
            PW_KEY_NODE_DESCRIPTION,  "Wolverine Microphone",
            NULL),
        &capture_events, NULL);
    pb = SPA_POD_BUILDER_INIT(buffer, sizeof(buffer));
    params[0] = build_format(&pb, in_rate, in_channels);
    pw_stream_connect(g.capture, PW_DIRECTION_OUTPUT, PW_ID_ANY,
        PW_STREAM_FLAG_AUTOCONNECT | PW_STREAM_FLAG_MAP_BUFFERS |
        PW_STREAM_FLAG_RT_PROCESS, params, 1);

    pw_thread_loop_unlock(g.loop);
    g.running = 1;
    return 0;
}

void wpw_stop(void) {
    if (!g.running) return;
    if (g.loop) pw_thread_loop_stop(g.loop);
    if (g.playback) pw_stream_destroy(g.playback);
    if (g.capture)  pw_stream_destroy(g.capture);
    if (g.core)     pw_core_disconnect(g.core);
    if (g.context)  pw_context_destroy(g.context);
    if (g.loop)     pw_thread_loop_destroy(g.loop);
    ring_free(&g.play_ring);
    ring_free(&g.cap_ring);
    g.running = 0;
}

// Bytes currently buffered in the playback ring (for priming / underrun logic).
int wpw_playback_avail(void) {
    if (!g.running) return 0;
    pthread_mutex_lock(&g.play_ring.lock);
    uint32_t a = g.play_ring.head - g.play_ring.tail;
    pthread_mutex_unlock(&g.play_ring.lock);
    return (int)a;
}

// Pull up to len bytes of playback audio (system -> USB). Returns bytes read.
int wpw_read_playback(void *dst, int len) {
    if (!g.running || len <= 0) return 0;
    return (int)ring_read(&g.play_ring, (uint8_t *)dst, (uint32_t)len);
}

// Push len bytes of captured mic audio (USB -> system). Returns len.
int wpw_write_capture(const void *src, int len) {
    if (!g.running || len <= 0) return 0;
    ring_write(&g.cap_ring, (const uint8_t *)src, (uint32_t)len);
    return len;
}
