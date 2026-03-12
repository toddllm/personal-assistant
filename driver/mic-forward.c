/*
 * mic-forward.c — Low-latency mic forwarding daemon
 *
 * Forwards audio from a physical microphone (Bose QC45 or MacBook Pro
 * Microphone) to the CaptureMic 2ch virtual device using Core Audio AUHAL
 * AudioUnits with callback-based IO.
 *
 * Replaces the Python scripts/mic-forward.py which suffered from blocking
 * writes (9ms avg, 147ms spikes) and GIL/GC pauses causing 60-90ms latency.
 * This C implementation targets <25ms total pipeline latency.
 *
 * Architecture:
 *   Physical Mic -> Input AUHAL render callback
 *     -> AudioConverterRef (resample if needed, e.g. 16kHz -> 48kHz)
 *     -> Mono-to-stereo upmix
 *     -> Lock-free SPSC ring buffer (65536 frames)
 *     -> Output AUHAL render callback -> CaptureMic 2ch
 *
 * Copyright (c) 2026 Todd Deshane. All rights reserved.
 */

#include <AudioToolbox/AudioToolbox.h>
#include <CoreAudio/CoreAudio.h>
#include <CoreFoundation/CoreFoundation.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

/* ───────────────────────────── Configuration ───────────────────────────── */

#define DEFAULT_PREFERRED_MIC   "MacBook Pro Microphone"
#define DEFAULT_FALLBACK_MIC    "MacBook Pro Microphone"
#define DEFAULT_TARGET_DEVICE   "CaptureMic 2ch"
#define TARGET_RATE             48000
#define TARGET_CHANNELS         2
#define RING_BUF_FRAMES         65536   /* ~1.36s at 48kHz, power of 2 */

/* ─────────────────────────── Lock-free Ring Buffer ──────────────────────── */

typedef struct {
    float       *buffer;        /* interleaved stereo: frames * 2 floats */
    uint32_t     capacity;      /* frame count (power of 2) */
    uint32_t     mask;          /* capacity - 1 */
    _Atomic uint32_t write_pos; /* written by input callback only */
    _Atomic uint32_t read_pos;  /* written by output callback only */
} RingBuffer;

static RingBuffer g_ring;

static void ring_init(RingBuffer *rb, uint32_t frame_capacity)
{
    rb->capacity = frame_capacity;
    rb->mask     = frame_capacity - 1;
    rb->buffer   = calloc(frame_capacity * TARGET_CHANNELS, sizeof(float));
    atomic_store(&rb->write_pos, 0);
    atomic_store(&rb->read_pos, 0);
}

static void ring_free(RingBuffer *rb)
{
    free(rb->buffer);
    rb->buffer = NULL;
}

static void ring_reset(RingBuffer *rb)
{
    atomic_store(&rb->write_pos, 0);
    atomic_store(&rb->read_pos, 0);
}

static uint32_t ring_available_read(const RingBuffer *rb)
{
    uint32_t w = atomic_load_explicit(&rb->write_pos, memory_order_acquire);
    uint32_t r = atomic_load_explicit(&rb->read_pos, memory_order_relaxed);
    return w - r;   /* works with unsigned wrap-around */
}

static uint32_t ring_available_write(const RingBuffer *rb)
{
    return rb->capacity - ring_available_read(rb);
}

static void ring_write(RingBuffer *rb, const float *data, uint32_t frames)
{
    uint32_t pos = atomic_load_explicit(&rb->write_pos, memory_order_relaxed);
    for (uint32_t i = 0; i < frames; i++) {
        uint32_t idx = (pos + i) & rb->mask;
        rb->buffer[idx * 2]     = data[i * 2];
        rb->buffer[idx * 2 + 1] = data[i * 2 + 1];
    }
    atomic_store_explicit(&rb->write_pos, pos + frames, memory_order_release);
}

static void ring_read(RingBuffer *rb, float *data, uint32_t frames)
{
    uint32_t pos = atomic_load_explicit(&rb->read_pos, memory_order_relaxed);
    for (uint32_t i = 0; i < frames; i++) {
        uint32_t idx = (pos + i) & rb->mask;
        data[i * 2]     = rb->buffer[idx * 2];
        data[i * 2 + 1] = rb->buffer[idx * 2 + 1];
    }
    atomic_store_explicit(&rb->read_pos, pos + frames, memory_order_release);
}

/* ──────────────────────────── Global State ──────────────────────────────── */

static volatile sig_atomic_t g_shutdown    = 0;
static volatile sig_atomic_t g_device_changed = 0;

static AudioComponentInstance g_input_au   = NULL;
static AudioComponentInstance g_output_au  = NULL;
static AudioConverterRef      g_converter  = NULL;

/* Runtime config (populated from env or defaults) */
static const char *g_preferred_mic;
static const char *g_fallback_mic;
static const char *g_target_device;

/* Active mic info (set during setup, read by callbacks) */
static AudioDeviceID g_input_device_id  = kAudioObjectUnknown;
static AudioDeviceID g_output_device_id = kAudioObjectUnknown;
static Float64       g_input_rate       = 0;
static UInt32        g_input_channels   = 0;
static bool          g_needs_resample   = false;
static bool          g_needs_upmix      = false;
static const char   *g_active_mic_name  = NULL;

/* Scratch buffer for resampled/upmixed audio in input callback */
#define MAX_CALLBACK_FRAMES 4096
static float g_scratch[MAX_CALLBACK_FRAMES * TARGET_CHANNELS];

/* Scratch buffer for converter input data callback */
static float         *g_converter_src     = NULL;
static UInt32         g_converter_src_frames = 0;
static UInt32         g_converter_src_pos  = 0;

/* Latency / stats tracking (updated by callbacks, read by main loop) */
static _Atomic uint64_t g_input_callbacks  = 0;
static _Atomic uint64_t g_output_callbacks = 0;
static _Atomic uint64_t g_input_frames     = 0;
static _Atomic uint64_t g_output_frames    = 0;
static _Atomic uint32_t g_output_underruns = 0;
static _Atomic uint32_t g_last_fill_level  = 0;  /* frames in ring at last output cb */

/* ──────────────────────────── Logging ───────────────────────────────────── */

static void log_msg(const char *level, const char *fmt, ...)
    __attribute__((format(printf, 2, 3)));

static void log_msg(const char *level, const char *fmt, ...)
{
    time_t now = time(NULL);
    struct tm tm;
    localtime_r(&now, &tm);
    char ts[20];
    strftime(ts, sizeof(ts), "%Y-%m-%d %H:%M:%S", &tm);

    fprintf(stderr, "%s [%s] ", ts, level);
    va_list ap;
    va_start(ap, fmt);
    vfprintf(stderr, fmt, ap);
    va_end(ap);
    fputc('\n', stderr);
}

#define LOG_INFO(...)    log_msg("INFO",    __VA_ARGS__)
#define LOG_WARN(...)    log_msg("WARNING", __VA_ARGS__)
#define LOG_ERROR(...)   log_msg("ERROR",   __VA_ARGS__)

/* ──────────────────────────── Signal Handler ────────────────────────────── */

static void signal_handler(int sig)
{
    (void)sig;
    g_shutdown = 1;
}

/* ──────────────────────────── Device Discovery ─────────────────────────── */

static AudioDeviceID find_device_by_name(const char *name, bool is_input)
{
    AudioObjectPropertyAddress addr = {
        .mSelector = kAudioHardwarePropertyDevices,
        .mScope    = kAudioObjectPropertyScopeGlobal,
        .mElement  = kAudioObjectPropertyElementMain,
    };

    UInt32 size = 0;
    OSStatus err = AudioObjectGetPropertyDataSize(
        kAudioObjectSystemObject, &addr, 0, NULL, &size);
    if (err != noErr || size == 0)
        return kAudioObjectUnknown;

    UInt32 count = size / sizeof(AudioDeviceID);
    AudioDeviceID *devices = malloc(size);
    if (!devices) return kAudioObjectUnknown;

    err = AudioObjectGetPropertyData(
        kAudioObjectSystemObject, &addr, 0, NULL, &size, devices);
    if (err != noErr) {
        free(devices);
        return kAudioObjectUnknown;
    }

    AudioDeviceID found = kAudioObjectUnknown;
    for (UInt32 i = 0; i < count; i++) {
        /* Check name */
        CFStringRef cf_name = NULL;
        UInt32 ns = sizeof(cf_name);
        AudioObjectPropertyAddress name_addr = {
            .mSelector = kAudioObjectPropertyName,
            .mScope    = kAudioObjectPropertyScopeGlobal,
            .mElement  = kAudioObjectPropertyElementMain,
        };
        err = AudioObjectGetPropertyData(devices[i], &name_addr, 0, NULL, &ns, &cf_name);
        if (err != noErr || !cf_name) continue;

        char buf[256];
        Boolean ok = CFStringGetCString(cf_name, buf, sizeof(buf),
                                        kCFStringEncodingUTF8);
        CFRelease(cf_name);
        if (!ok) continue;

        /* Case-insensitive substring match (same logic as Python version) */
        char name_lower[256], buf_lower[256];
        for (int j = 0; name[j] && j < 255; j++)
            name_lower[j] = (char)tolower((unsigned char)name[j]);
        name_lower[strlen(name) < 255 ? strlen(name) : 255] = '\0';
        for (int j = 0; buf[j] && j < 255; j++)
            buf_lower[j] = (char)tolower((unsigned char)buf[j]);
        buf_lower[strlen(buf) < 255 ? strlen(buf) : 255] = '\0';

        if (!strstr(buf_lower, name_lower)) continue;

        /* Check direction: device must have streams in the requested scope */
        AudioObjectPropertyAddress stream_addr = {
            .mSelector = kAudioDevicePropertyStreams,
            .mScope    = is_input ? kAudioDevicePropertyScopeInput
                                  : kAudioDevicePropertyScopeOutput,
            .mElement  = kAudioObjectPropertyElementMain,
        };
        UInt32 stream_size = 0;
        err = AudioObjectGetPropertyDataSize(
            devices[i], &stream_addr, 0, NULL, &stream_size);
        if (err != noErr || stream_size == 0) continue;

        found = devices[i];
        break;
    }

    free(devices);
    return found;
}

static bool get_device_sample_rate(AudioDeviceID dev, Float64 *rate)
{
    AudioObjectPropertyAddress addr = {
        .mSelector = kAudioDevicePropertyNominalSampleRate,
        .mScope    = kAudioObjectPropertyScopeGlobal,
        .mElement  = kAudioObjectPropertyElementMain,
    };
    UInt32 size = sizeof(*rate);
    return AudioObjectGetPropertyData(dev, &addr, 0, NULL, &size, rate) == noErr;
}

static bool get_device_channel_count(AudioDeviceID dev, bool is_input, UInt32 *count)
{
    AudioObjectPropertyAddress addr = {
        .mSelector = kAudioDevicePropertyStreamConfiguration,
        .mScope    = is_input ? kAudioDevicePropertyScopeInput
                              : kAudioDevicePropertyScopeOutput,
        .mElement  = kAudioObjectPropertyElementMain,
    };
    UInt32 size = 0;
    OSStatus err = AudioObjectGetPropertyDataSize(dev, &addr, 0, NULL, &size);
    if (err != noErr) return false;

    AudioBufferList *abl = malloc(size);
    if (!abl) return false;

    err = AudioObjectGetPropertyData(dev, &addr, 0, NULL, &size, abl);
    if (err != noErr) { free(abl); return false; }

    UInt32 total = 0;
    for (UInt32 i = 0; i < abl->mNumberBuffers; i++)
        total += abl->mBuffers[i].mNumberChannels;

    free(abl);
    *count = total;
    return true;
}

/* ──────────────────────── Hot-Plug Listener ─────────────────────────────── */

static OSStatus device_list_changed(
    AudioObjectID                       inObjectID,
    UInt32                              inNumberAddresses,
    const AudioObjectPropertyAddress    inAddresses[],
    void                               *inClientData)
{
    (void)inObjectID; (void)inNumberAddresses;
    (void)inAddresses; (void)inClientData;
    g_device_changed = 1;
    return noErr;
}

static void install_hotplug_listener(void)
{
    AudioObjectPropertyAddress addr = {
        .mSelector = kAudioHardwarePropertyDevices,
        .mScope    = kAudioObjectPropertyScopeGlobal,
        .mElement  = kAudioObjectPropertyElementMain,
    };
    AudioObjectAddPropertyListener(
        kAudioObjectSystemObject, &addr, device_list_changed, NULL);
}

static void remove_hotplug_listener(void)
{
    AudioObjectPropertyAddress addr = {
        .mSelector = kAudioHardwarePropertyDevices,
        .mScope    = kAudioObjectPropertyScopeGlobal,
        .mElement  = kAudioObjectPropertyElementMain,
    };
    AudioObjectRemovePropertyListener(
        kAudioObjectSystemObject, &addr, device_list_changed, NULL);
}

/* ──────────────── AudioConverter Data Supplier Callback ──────────────────── */

static OSStatus converter_input_callback(
    AudioConverterRef             inAudioConverter,
    UInt32                       *ioNumberDataPackets,
    AudioBufferList              *ioData,
    AudioStreamPacketDescription **outDataPacketDescription,
    void                         *inUserData)
{
    (void)inAudioConverter; (void)outDataPacketDescription; (void)inUserData;

    UInt32 remaining = g_converter_src_frames - g_converter_src_pos;
    if (remaining == 0) {
        *ioNumberDataPackets = 0;
        return noErr;
    }

    UInt32 frames = *ioNumberDataPackets;
    if (frames > remaining)
        frames = remaining;

    ioData->mNumberBuffers = 1;
    ioData->mBuffers[0].mNumberChannels = g_input_channels;
    ioData->mBuffers[0].mDataByteSize   = frames * g_input_channels * sizeof(float);
    ioData->mBuffers[0].mData           = g_converter_src + g_converter_src_pos * g_input_channels;

    g_converter_src_pos += frames;
    *ioNumberDataPackets = frames;
    return noErr;
}

/* ──────────────────── Input Render Callback ──────────────────────────────── */

static OSStatus input_render_callback(
    void                        *inRefCon,
    AudioUnitRenderActionFlags  *ioActionFlags,
    const AudioTimeStamp        *inTimeStamp,
    UInt32                       inBusNumber,
    UInt32                       inNumberFrames,
    AudioBufferList             *ioData)
{
    (void)inRefCon;

    if (g_shutdown) return noErr;

    /* Allocate a temporary ABL on the stack for pulling mic data */
    UInt32 src_channels = g_input_channels;
    float raw_buf[MAX_CALLBACK_FRAMES * 2]; /* max 2 channels from mic */
    AudioBufferList abl;
    abl.mNumberBuffers = 1;
    abl.mBuffers[0].mNumberChannels = src_channels;
    abl.mBuffers[0].mDataByteSize   = inNumberFrames * src_channels * sizeof(float);
    abl.mBuffers[0].mData           = raw_buf;

    OSStatus err = AudioUnitRender(g_input_au, ioActionFlags, inTimeStamp,
                                   inBusNumber, inNumberFrames, &abl);
    if (err != noErr) return err;

    float *src    = raw_buf;
    uint32_t frames = inNumberFrames;

    /* --- Resample if needed (e.g. Bose 16kHz -> 48kHz) --- */
    float resampled_buf[MAX_CALLBACK_FRAMES * 2];
    if (g_needs_resample && g_converter) {
        /* Compute expected output frames */
        double ratio = (double)TARGET_RATE / g_input_rate;
        UInt32 out_frames = (UInt32)(inNumberFrames * ratio + 0.5);
        if (out_frames > MAX_CALLBACK_FRAMES)
            out_frames = MAX_CALLBACK_FRAMES;

        /* Set up converter input state */
        g_converter_src        = src;
        g_converter_src_frames = inNumberFrames;
        g_converter_src_pos    = 0;

        AudioBufferList out_abl;
        out_abl.mNumberBuffers = 1;
        out_abl.mBuffers[0].mNumberChannels = src_channels;
        out_abl.mBuffers[0].mDataByteSize   = out_frames * src_channels * sizeof(float);
        out_abl.mBuffers[0].mData           = resampled_buf;

        UInt32 out_packets = out_frames;
        err = AudioConverterFillComplexBuffer(
            g_converter, converter_input_callback, NULL,
            &out_packets, &out_abl, NULL);

        if (err == noErr) {
            src    = resampled_buf;
            frames = out_packets;
        }
        /* On other errors, fall through with original data */
    }

    /* --- Mono -> Stereo upmix + write to ring buffer --- */
    if (frames > MAX_CALLBACK_FRAMES) frames = MAX_CALLBACK_FRAMES;

    if (g_needs_upmix) {
        /* src is mono (1 float per frame), write stereo to scratch */
        for (uint32_t i = 0; i < frames; i++) {
            g_scratch[i * 2]     = src[i];
            g_scratch[i * 2 + 1] = src[i];
        }
    } else {
        /* Already stereo — copy directly */
        memcpy(g_scratch, src, frames * TARGET_CHANNELS * sizeof(float));
    }

    /* Write to ring buffer (drop oldest if full) */
    if (ring_available_write(&g_ring) >= frames) {
        ring_write(&g_ring, g_scratch, frames);
    }
    /* else: ring full, drop this block (shouldn't happen with 65536 frames) */

    atomic_fetch_add_explicit(&g_input_callbacks, 1, memory_order_relaxed);
    atomic_fetch_add_explicit(&g_input_frames, frames, memory_order_relaxed);

    return noErr;
}

/* ──────────────────── Output Render Callback ────────────────────────────── */

static OSStatus output_render_callback(
    void                        *inRefCon,
    AudioUnitRenderActionFlags  *ioActionFlags,
    const AudioTimeStamp        *inTimeStamp,
    UInt32                       inBusNumber,
    UInt32                       inNumberFrames,
    AudioBufferList             *ioData)
{
    (void)inRefCon; (void)ioActionFlags;
    (void)inTimeStamp; (void)inBusNumber;

    if (!ioData || ioData->mNumberBuffers == 0) return noErr;

    float *out = (float *)ioData->mBuffers[0].mData;
    uint32_t avail = ring_available_read(&g_ring);

    atomic_store_explicit(&g_last_fill_level, avail, memory_order_relaxed);

    if (avail >= inNumberFrames) {
        ring_read(&g_ring, out, inNumberFrames);
    } else {
        /* Underrun: read what we have, fill rest with silence */
        if (avail > 0)
            ring_read(&g_ring, out, avail);
        memset(out + avail * TARGET_CHANNELS, 0,
               (inNumberFrames - avail) * TARGET_CHANNELS * sizeof(float));
        atomic_fetch_add_explicit(&g_output_underruns, 1, memory_order_relaxed);
    }

    atomic_fetch_add_explicit(&g_output_callbacks, 1, memory_order_relaxed);
    atomic_fetch_add_explicit(&g_output_frames, inNumberFrames, memory_order_relaxed);

    return noErr;
}

/* ──────────────────── AudioUnit Setup / Teardown ────────────────────────── */

static void teardown_audio(void)
{
    if (g_input_au) {
        AudioOutputUnitStop(g_input_au);
        AudioComponentInstanceDispose(g_input_au);
        g_input_au = NULL;
    }
    if (g_output_au) {
        AudioOutputUnitStop(g_output_au);
        AudioComponentInstanceDispose(g_output_au);
        g_output_au = NULL;
    }
    if (g_converter) {
        AudioConverterDispose(g_converter);
        g_converter = NULL;
    }
    ring_reset(&g_ring);
}

static bool setup_audio(void)
{
    OSStatus err;

    /* ---- Find input device ---- */
    AudioDeviceID preferred = find_device_by_name(g_preferred_mic, true);
    AudioDeviceID fallback  = find_device_by_name(g_fallback_mic, true);

    if (preferred != kAudioObjectUnknown) {
        g_input_device_id = preferred;
        g_active_mic_name = g_preferred_mic;
    } else if (fallback != kAudioObjectUnknown) {
        g_input_device_id = fallback;
        g_active_mic_name = g_fallback_mic;
    } else {
        LOG_WARN("no mic found (tried '%s', '%s')", g_preferred_mic, g_fallback_mic);
        return false;
    }

    /* ---- Find output device (CaptureMic 2ch) ---- */
    g_output_device_id = find_device_by_name(g_target_device, false);
    if (g_output_device_id == kAudioObjectUnknown) {
        LOG_WARN("target '%s' not found", g_target_device);
        return false;
    }

    /* ---- Query input device properties ---- */
    if (!get_device_sample_rate(g_input_device_id, &g_input_rate)) {
        LOG_ERROR("failed to get sample rate for '%s'", g_active_mic_name);
        return false;
    }
    if (!get_device_channel_count(g_input_device_id, true, &g_input_channels)) {
        LOG_ERROR("failed to get channel count for '%s'", g_active_mic_name);
        return false;
    }
    if (g_input_channels == 0) g_input_channels = 1;
    if (g_input_channels > 2) g_input_channels = 2; /* only use first 2 */

    g_needs_resample = ((UInt32)g_input_rate != TARGET_RATE);
    g_needs_upmix   = (g_input_channels < TARGET_CHANNELS);

    LOG_INFO("input: %s (%dch @ %.0f Hz)%s%s",
             g_active_mic_name, g_input_channels, g_input_rate,
             g_needs_resample ? " [resample]" : "",
             g_needs_upmix ? " [upmix]" : "");

    /* ---- Create AudioConverter for resampling if needed ---- */
    if (g_needs_resample) {
        AudioStreamBasicDescription in_fmt = {
            .mSampleRate       = g_input_rate,
            .mFormatID         = kAudioFormatLinearPCM,
            .mFormatFlags      = kAudioFormatFlagIsFloat | kAudioFormatFlagIsPacked,
            .mBytesPerPacket   = (UInt32)(g_input_channels * sizeof(float)),
            .mFramesPerPacket  = 1,
            .mBytesPerFrame    = (UInt32)(g_input_channels * sizeof(float)),
            .mChannelsPerFrame = g_input_channels,
            .mBitsPerChannel   = 32,
        };
        AudioStreamBasicDescription out_fmt = in_fmt;
        out_fmt.mSampleRate = TARGET_RATE;

        err = AudioConverterNew(&in_fmt, &out_fmt, &g_converter);
        if (err != noErr) {
            LOG_ERROR("AudioConverterNew failed: %d", (int)err);
            return false;
        }

        /* Use normal quality polyphase sinc resampler */
        UInt32 quality = kAudioConverterQuality_Medium;
        AudioConverterSetProperty(g_converter,
            kAudioConverterSampleRateConverterQuality,
            sizeof(quality), &quality);
    }

    /* ---- Setup Input AUHAL ---- */
    AudioComponentDescription au_desc = {
        .componentType         = kAudioUnitType_Output,
        .componentSubType      = kAudioUnitSubType_HALOutput,
        .componentManufacturer = kAudioUnitManufacturer_Apple,
    };
    AudioComponent comp = AudioComponentFindNext(NULL, &au_desc);
    if (!comp) {
        LOG_ERROR("AUHAL component not found");
        return false;
    }

    err = AudioComponentInstanceNew(comp, &g_input_au);
    if (err != noErr) {
        LOG_ERROR("failed to create input AUHAL: %d", (int)err);
        return false;
    }

    /* Enable input on bus 1, disable output on bus 0 */
    UInt32 enable = 1, disable = 0;
    AudioUnitSetProperty(g_input_au, kAudioOutputUnitProperty_EnableIO,
                         kAudioUnitScope_Input, 1, &enable, sizeof(enable));
    AudioUnitSetProperty(g_input_au, kAudioOutputUnitProperty_EnableIO,
                         kAudioUnitScope_Output, 0, &disable, sizeof(disable));

    /* Set input device */
    AudioUnitSetProperty(g_input_au, kAudioOutputUnitProperty_CurrentDevice,
                         kAudioUnitScope_Global, 0,
                         &g_input_device_id, sizeof(g_input_device_id));

    /* Set stream format on output scope of bus 1 (what we receive in callback) */
    AudioStreamBasicDescription input_fmt = {
        .mSampleRate       = g_input_rate,
        .mFormatID         = kAudioFormatLinearPCM,
        .mFormatFlags      = kAudioFormatFlagIsFloat | kAudioFormatFlagIsPacked,
        .mBytesPerPacket   = (UInt32)(g_input_channels * sizeof(float)),
        .mFramesPerPacket  = 1,
        .mBytesPerFrame    = (UInt32)(g_input_channels * sizeof(float)),
        .mChannelsPerFrame = g_input_channels,
        .mBitsPerChannel   = 32,
    };
    AudioUnitSetProperty(g_input_au, kAudioUnitProperty_StreamFormat,
                         kAudioUnitScope_Output, 1,
                         &input_fmt, sizeof(input_fmt));

    /* Set input callback */
    AURenderCallbackStruct input_cb = {
        .inputProc       = input_render_callback,
        .inputProcRefCon = NULL,
    };
    AudioUnitSetProperty(g_input_au, kAudioOutputUnitProperty_SetInputCallback,
                         kAudioUnitScope_Global, 0,
                         &input_cb, sizeof(input_cb));

    err = AudioUnitInitialize(g_input_au);
    if (err != noErr) {
        LOG_ERROR("AudioUnitInitialize (input) failed: %d", (int)err);
        teardown_audio();
        return false;
    }

    /* ---- Setup Output AUHAL ---- */
    err = AudioComponentInstanceNew(comp, &g_output_au);
    if (err != noErr) {
        LOG_ERROR("failed to create output AUHAL: %d", (int)err);
        teardown_audio();
        return false;
    }

    /* Output only (default), disable input */
    AudioUnitSetProperty(g_output_au, kAudioOutputUnitProperty_EnableIO,
                         kAudioUnitScope_Input, 1, &disable, sizeof(disable));
    AudioUnitSetProperty(g_output_au, kAudioOutputUnitProperty_EnableIO,
                         kAudioUnitScope_Output, 0, &enable, sizeof(enable));

    /* Set output device */
    AudioUnitSetProperty(g_output_au, kAudioOutputUnitProperty_CurrentDevice,
                         kAudioUnitScope_Global, 0,
                         &g_output_device_id, sizeof(g_output_device_id));

    /* Set stream format on input scope of bus 0 (what we supply) */
    AudioStreamBasicDescription output_fmt = {
        .mSampleRate       = TARGET_RATE,
        .mFormatID         = kAudioFormatLinearPCM,
        .mFormatFlags      = kAudioFormatFlagIsFloat | kAudioFormatFlagIsPacked,
        .mBytesPerPacket   = TARGET_CHANNELS * sizeof(float),
        .mFramesPerPacket  = 1,
        .mBytesPerFrame    = TARGET_CHANNELS * sizeof(float),
        .mChannelsPerFrame = TARGET_CHANNELS,
        .mBitsPerChannel   = 32,
    };
    AudioUnitSetProperty(g_output_au, kAudioUnitProperty_StreamFormat,
                         kAudioUnitScope_Input, 0,
                         &output_fmt, sizeof(output_fmt));

    /* Set render callback */
    AURenderCallbackStruct output_cb = {
        .inputProc       = output_render_callback,
        .inputProcRefCon = NULL,
    };
    AudioUnitSetProperty(g_output_au, kAudioUnitProperty_SetRenderCallback,
                         kAudioUnitScope_Input, 0,
                         &output_cb, sizeof(output_cb));

    err = AudioUnitInitialize(g_output_au);
    if (err != noErr) {
        LOG_ERROR("AudioUnitInitialize (output) failed: %d", (int)err);
        teardown_audio();
        return false;
    }

    /* ---- Start both AudioUnits ---- */
    err = AudioOutputUnitStart(g_input_au);
    if (err != noErr) {
        LOG_ERROR("AudioOutputUnitStart (input) failed: %d", (int)err);
        teardown_audio();
        return false;
    }
    err = AudioOutputUnitStart(g_output_au);
    if (err != noErr) {
        LOG_ERROR("AudioOutputUnitStart (output) failed: %d", (int)err);
        teardown_audio();
        return false;
    }

    LOG_INFO("active: %s -> %s", g_active_mic_name, g_target_device);
    return true;
}

/* ──────────────────────────── Main Loop ─────────────────────────────────── */

static void main_loop(void)
{
    bool active = false;
    int stats_countdown = 0; /* log stats on first active iteration */

    while (!g_shutdown) {
        /* Periodic stats logging (every ~10s when active) */
        if (active && --stats_countdown <= 0) {
            stats_countdown = 50; /* 50 * 200ms = 10s */
            uint32_t fill = atomic_load_explicit(&g_last_fill_level,
                                                 memory_order_relaxed);
            uint64_t in_cb  = atomic_load(&g_input_callbacks);
            uint64_t out_cb = atomic_load(&g_output_callbacks);
            uint64_t in_f   = atomic_load(&g_input_frames);
            uint64_t out_f  = atomic_load(&g_output_frames);
            uint32_t ur     = atomic_load(&g_output_underruns);
            double fill_ms  = (double)fill / TARGET_RATE * 1000.0;
            LOG_INFO("stats: ring=%u frames (%.1f ms), "
                     "in=%llu cb/%llu frames, out=%llu cb/%llu frames, "
                     "underruns=%u",
                     fill, fill_ms, in_cb, in_f, out_cb, out_f, ur);
        }

        /* Handle device change event */
        if (g_device_changed) {
            g_device_changed = 0;

            /* Check if preferred mic appeared/disappeared */
            AudioDeviceID preferred = find_device_by_name(g_preferred_mic, true);
            bool preferred_available = (preferred != kAudioObjectUnknown);

            if (active) {
                /* Currently using fallback — switch if preferred appeared */
                if (g_active_mic_name == g_fallback_mic && preferred_available) {
                    LOG_INFO("preferred mic '%s' detected, switching", g_preferred_mic);
                    teardown_audio();
                    active = false;
                    /* Fall through to setup with preferred */
                }
                /* Currently using preferred — switch to fallback if it disappeared */
                else if (g_active_mic_name == g_preferred_mic && !preferred_available) {
                    LOG_WARN("mic '%s' disconnected", g_preferred_mic);
                    teardown_audio();
                    active = false;
                    /* Fall through to setup with fallback */
                }
                /* Check if current mic is still available */
                else if (find_device_by_name(g_active_mic_name, true) == kAudioObjectUnknown) {
                    LOG_WARN("mic '%s' disappeared", g_active_mic_name);
                    teardown_audio();
                    active = false;
                }
            }
        }

        if (!active) {
            active = setup_audio();
            if (!active) {
                /* Retry in 2 seconds */
                for (int i = 0; i < 20 && !g_shutdown && !g_device_changed; i++)
                    usleep(100000); /* 100ms * 20 = 2s */
                continue;
            }
        }

        /* Sleep 200ms then check for events */
        for (int i = 0; i < 2 && !g_shutdown && !g_device_changed; i++)
            usleep(100000);
    }
}

/* ──────────────────────────── Config ────────────────────────────────────── */

static const char *env_or_default(const char *env_var, const char *def)
{
    const char *val = getenv(env_var);
    return (val && val[0]) ? val : def;
}

/* ──────────────────────────── Entry Point ───────────────────────────────── */

int main(int argc, char *argv[])
{
    (void)argc; (void)argv;

    /* Load config from environment */
    g_preferred_mic = env_or_default("AUDIO_ASSIST_MIC_FORWARD_PREFERRED_MIC",
                                     DEFAULT_PREFERRED_MIC);
    g_fallback_mic  = env_or_default("AUDIO_ASSIST_MIC_FORWARD_FALLBACK_MIC",
                                     DEFAULT_FALLBACK_MIC);
    g_target_device = env_or_default("AUDIO_ASSIST_MIC_FORWARD_TARGET_DEVICE",
                                     DEFAULT_TARGET_DEVICE);

    LOG_INFO("mic-forward starting");
    LOG_INFO("  target: %s (%dch @ %d Hz)", g_target_device, TARGET_CHANNELS, TARGET_RATE);
    LOG_INFO("  preferred mic: %s", g_preferred_mic);
    LOG_INFO("  fallback mic: %s", g_fallback_mic);

    /* Init ring buffer */
    ring_init(&g_ring, RING_BUF_FRAMES);

    /* Signal handling */
    struct sigaction sa = { .sa_handler = signal_handler };
    sigemptyset(&sa.sa_mask);
    sigaction(SIGTERM, &sa, NULL);
    sigaction(SIGINT, &sa, NULL);

    /* Install hot-plug listener */
    install_hotplug_listener();

    /* Run */
    main_loop();

    /* Cleanup */
    LOG_INFO("mic-forward shutting down");
    teardown_audio();
    remove_hotplug_listener();
    ring_free(&g_ring);
    LOG_INFO("mic-forward exited");

    return 0;
}
