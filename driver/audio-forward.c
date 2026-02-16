/*
 * audio-forward.c — Low-latency audio forwarding daemon
 *
 * Forwards audio from CaptureAudio 2ch virtual device to all connected
 * physical output devices (MacBook Pro Speakers, Bose QC45, etc.) using
 * Core Audio AUHAL AudioUnits with callback-based IO.
 *
 * Replaces the Python scripts/audio-forward.py which suffered from
 * PortAudio output buffering (~200-500ms on Bluetooth A2DP).
 *
 * Architecture:
 *   CaptureAudio 2ch -> Input AUHAL render callback
 *     -> Lock-free SPSC ring buffer (per output device)
 *     -> Per-output AUHAL render callback
 *       -> AudioConverterRef if resample needed (48kHz -> 44.1kHz for BT)
 *       -> Physical speaker/headphone
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
#include <ctype.h>
#include <pthread.h>

/* ───────────────────────────── Configuration ───────────────────────────── */

#define DEFAULT_SOURCE_DEVICE   "CaptureAudio 2ch"
#define SOURCE_RATE             48000
#define SOURCE_CHANNELS         2
#define RING_BUF_FRAMES         65536   /* ~1.36s at 48kHz, power of 2 */
#define MAX_OUTPUTS             8
#define DEVICE_SCAN_INTERVAL_S  5
#define MAX_CALLBACK_FRAMES     4096

/* Excluded device name substrings (virtual/loopback devices) */
static const char *EXCLUDED[] = {
    "capturemic", "captureaudio", "blackhole",
    "aggregate device", "multi-output device",
    "zoomaudiodevice", "cluely", "microsoft teams audio",
    NULL
};

/* ─────────────────────────── Lock-free Ring Buffer ──────────────────────── */

typedef struct {
    float       *buffer;
    uint32_t     capacity;
    uint32_t     mask;
    _Atomic uint32_t write_pos;
    _Atomic uint32_t read_pos;
} RingBuffer;

static void ring_init(RingBuffer *rb, uint32_t frame_capacity)
{
    rb->capacity = frame_capacity;
    rb->mask     = frame_capacity - 1;
    rb->buffer   = calloc(frame_capacity * SOURCE_CHANNELS, sizeof(float));
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
    return w - r;
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
        rb->buffer[idx * SOURCE_CHANNELS]     = data[i * SOURCE_CHANNELS];
        rb->buffer[idx * SOURCE_CHANNELS + 1] = data[i * SOURCE_CHANNELS + 1];
    }
    atomic_store_explicit(&rb->write_pos, pos + frames, memory_order_release);
}

static void ring_read(RingBuffer *rb, float *data, uint32_t frames, int out_channels)
{
    uint32_t pos = atomic_load_explicit(&rb->read_pos, memory_order_relaxed);
    for (uint32_t i = 0; i < frames; i++) {
        uint32_t idx = (pos + i) & rb->mask;
        if (out_channels == 2) {
            data[i * 2]     = rb->buffer[idx * 2];
            data[i * 2 + 1] = rb->buffer[idx * 2 + 1];
        } else {
            /* downmix to mono */
            data[i] = (rb->buffer[idx * 2] + rb->buffer[idx * 2 + 1]) * 0.5f;
        }
    }
    atomic_store_explicit(&rb->read_pos, pos + frames, memory_order_release);
}

/* ──────────────────────────── Per-Output State ──────────────────────────── */

typedef struct {
    char                    name[256];
    AudioDeviceID           device_id;
    AudioComponentInstance  output_au;
    AudioConverterRef       converter;
    RingBuffer              ring;
    Float64                 device_rate;
    UInt32                  device_channels;
    bool                    needs_resample;
    bool                    active;
    _Atomic uint32_t        underruns;

    /* Converter data-supplier state (accessed from render callback) */
    float                  *conv_src;
    UInt32                  conv_src_frames;
    UInt32                  conv_src_pos;
    UInt32                  conv_src_channels;
} OutputSlot;

/* ──────────────────────────── Global State ──────────────────────────────── */

static volatile sig_atomic_t g_shutdown = 0;

static AudioComponentInstance g_input_au = NULL;
static AudioDeviceID g_input_device_id   = kAudioObjectUnknown;
static const char *g_source_device;

static OutputSlot g_outputs[MAX_OUTPUTS];
static int        g_output_count = 0;
static pthread_mutex_t g_outputs_lock = PTHREAD_MUTEX_INITIALIZER;

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
    fflush(stderr);
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

static bool str_contains_lower(const char *haystack, const char *needle)
{
    char h[512], n[512];
    for (int i = 0; haystack[i] && i < 511; i++)
        h[i] = (char)tolower((unsigned char)haystack[i]);
    h[strlen(haystack) < 511 ? strlen(haystack) : 511] = '\0';
    for (int i = 0; needle[i] && i < 511; i++)
        n[i] = (char)tolower((unsigned char)needle[i]);
    n[strlen(needle) < 511 ? strlen(needle) : 511] = '\0';
    return strstr(h, n) != NULL;
}

static bool is_excluded(const char *name)
{
    for (int i = 0; EXCLUDED[i]; i++) {
        if (str_contains_lower(name, EXCLUDED[i]))
            return true;
    }
    return false;
}

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
    if (err != noErr || size == 0) return kAudioObjectUnknown;

    UInt32 count = size / sizeof(AudioDeviceID);
    AudioDeviceID *devices = malloc(size);
    if (!devices) return kAudioObjectUnknown;

    err = AudioObjectGetPropertyData(
        kAudioObjectSystemObject, &addr, 0, NULL, &size, devices);
    if (err != noErr) { free(devices); return kAudioObjectUnknown; }

    AudioDeviceID found = kAudioObjectUnknown;
    for (UInt32 i = 0; i < count; i++) {
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
        Boolean ok = CFStringGetCString(cf_name, buf, sizeof(buf), kCFStringEncodingUTF8);
        CFRelease(cf_name);
        if (!ok) continue;

        if (!str_contains_lower(buf, name)) continue;

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

static bool get_device_name(AudioDeviceID dev, char *buf, size_t buflen)
{
    CFStringRef cf_name = NULL;
    UInt32 size = sizeof(cf_name);
    AudioObjectPropertyAddress addr = {
        .mSelector = kAudioObjectPropertyName,
        .mScope    = kAudioObjectPropertyScopeGlobal,
        .mElement  = kAudioObjectPropertyElementMain,
    };
    OSStatus err = AudioObjectGetPropertyData(dev, &addr, 0, NULL, &size, &cf_name);
    if (err != noErr || !cf_name) return false;
    Boolean ok = CFStringGetCString(cf_name, buf, (CFIndex)buflen, kCFStringEncodingUTF8);
    CFRelease(cf_name);
    return ok;
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

/* Discover all output devices, returns count. Fills device_ids array. */
static int discover_output_devices(AudioDeviceID *out_ids, char names[][256], int max)
{
    AudioObjectPropertyAddress addr = {
        .mSelector = kAudioHardwarePropertyDevices,
        .mScope    = kAudioObjectPropertyScopeGlobal,
        .mElement  = kAudioObjectPropertyElementMain,
    };
    UInt32 size = 0;
    OSStatus err = AudioObjectGetPropertyDataSize(
        kAudioObjectSystemObject, &addr, 0, NULL, &size);
    if (err != noErr || size == 0) return 0;

    UInt32 count = size / sizeof(AudioDeviceID);
    AudioDeviceID *devices = malloc(size);
    if (!devices) return 0;

    err = AudioObjectGetPropertyData(
        kAudioObjectSystemObject, &addr, 0, NULL, &size, devices);
    if (err != noErr) { free(devices); return 0; }

    int found = 0;
    for (UInt32 i = 0; i < count && found < max; i++) {
        /* Check it has output channels */
        UInt32 ch = 0;
        if (!get_device_channel_count(devices[i], false, &ch) || ch == 0)
            continue;

        char name[256];
        if (!get_device_name(devices[i], name, sizeof(name)))
            continue;

        if (is_excluded(name))
            continue;

        out_ids[found] = devices[i];
        strncpy(names[found], name, 255);
        names[found][255] = '\0';
        found++;
    }

    free(devices);
    return found;
}

/* ──────────── AudioConverter Data Supplier (per output slot) ────────────── */

static OSStatus converter_input_callback(
    AudioConverterRef             inConverter,
    UInt32                       *ioNumberDataPackets,
    AudioBufferList              *ioData,
    AudioStreamPacketDescription **outDesc,
    void                         *inUserData)
{
    (void)inConverter; (void)outDesc;
    OutputSlot *slot = (OutputSlot *)inUserData;

    UInt32 remaining = slot->conv_src_frames - slot->conv_src_pos;
    if (remaining == 0) {
        *ioNumberDataPackets = 0;
        return noErr;
    }

    UInt32 frames = *ioNumberDataPackets;
    if (frames > remaining) frames = remaining;

    ioData->mNumberBuffers = 1;
    ioData->mBuffers[0].mNumberChannels = slot->conv_src_channels;
    ioData->mBuffers[0].mDataByteSize   = frames * slot->conv_src_channels * sizeof(float);
    ioData->mBuffers[0].mData           = slot->conv_src + slot->conv_src_pos * slot->conv_src_channels;

    slot->conv_src_pos += frames;
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

    float raw_buf[MAX_CALLBACK_FRAMES * SOURCE_CHANNELS];
    AudioBufferList abl;
    abl.mNumberBuffers = 1;
    abl.mBuffers[0].mNumberChannels = SOURCE_CHANNELS;
    abl.mBuffers[0].mDataByteSize   = inNumberFrames * SOURCE_CHANNELS * sizeof(float);
    abl.mBuffers[0].mData           = raw_buf;

    OSStatus err = AudioUnitRender(g_input_au, ioActionFlags, inTimeStamp,
                                   inBusNumber, inNumberFrames, &abl);
    if (err != noErr) return err;

    if (inNumberFrames > MAX_CALLBACK_FRAMES)
        inNumberFrames = MAX_CALLBACK_FRAMES;

    /* Distribute to all active output ring buffers.
     * No mutex here — real-time audio callback must be lock-free.
     * g_output_count is only increased (never decreased while running)
     * and slot->active is set atomically before starting the output AU. */
    int count = g_output_count; /* snapshot */
    for (int i = 0; i < count; i++) {
        if (!g_outputs[i].active) continue;
        if (ring_available_write(&g_outputs[i].ring) >= inNumberFrames) {
            ring_write(&g_outputs[i].ring, raw_buf, inNumberFrames);
        }
    }

    return noErr;
}

/* ──────────────── Per-Output Render Callback ────────────────────────────── */

static OSStatus output_render_callback(
    void                        *inRefCon,
    AudioUnitRenderActionFlags  *ioActionFlags,
    const AudioTimeStamp        *inTimeStamp,
    UInt32                       inBusNumber,
    UInt32                       inNumberFrames,
    AudioBufferList             *ioData)
{
    (void)ioActionFlags; (void)inTimeStamp; (void)inBusNumber;
    OutputSlot *slot = (OutputSlot *)inRefCon;

    if (!ioData || ioData->mNumberBuffers == 0) return noErr;

    float *out = (float *)ioData->mBuffers[0].mData;
    UInt32 out_channels = slot->device_channels;

    if (slot->needs_resample && slot->converter) {
        /* Read from ring at source rate, resample to device rate */
        double ratio = (double)SOURCE_RATE / slot->device_rate;
        UInt32 src_frames_needed = (UInt32)(inNumberFrames * ratio + 1);
        if (src_frames_needed > MAX_CALLBACK_FRAMES)
            src_frames_needed = MAX_CALLBACK_FRAMES;

        uint32_t avail = ring_available_read(&slot->ring);
        if (avail < src_frames_needed) {
            /* Underrun */
            memset(out, 0, inNumberFrames * out_channels * sizeof(float));
            atomic_fetch_add_explicit(&slot->underruns, 1, memory_order_relaxed);
            return noErr;
        }

        float src_buf[MAX_CALLBACK_FRAMES * SOURCE_CHANNELS];
        ring_read(&slot->ring, src_buf, src_frames_needed, SOURCE_CHANNELS);

        /* Set up converter source */
        slot->conv_src          = src_buf;
        slot->conv_src_frames   = src_frames_needed;
        slot->conv_src_pos      = 0;
        slot->conv_src_channels = SOURCE_CHANNELS;

        /* Convert: resample + channel adjustment */
        AudioBufferList out_abl;
        out_abl.mNumberBuffers = 1;
        out_abl.mBuffers[0].mNumberChannels = out_channels;
        out_abl.mBuffers[0].mDataByteSize   = inNumberFrames * out_channels * sizeof(float);
        out_abl.mBuffers[0].mData           = out;

        UInt32 out_packets = inNumberFrames;
        OSStatus cerr = AudioConverterFillComplexBuffer(
            slot->converter, converter_input_callback, slot,
            &out_packets, &out_abl, NULL);
        if (cerr != noErr && cerr != 1) {
            memset(out, 0, inNumberFrames * out_channels * sizeof(float));
        }
    } else {
        /* No resample needed — direct read from ring */
        uint32_t avail = ring_available_read(&slot->ring);
        if (avail >= inNumberFrames) {
            ring_read(&slot->ring, out, inNumberFrames, out_channels);
        } else {
            if (avail > 0)
                ring_read(&slot->ring, out, avail, out_channels);
            memset(out + avail * out_channels, 0,
                   (inNumberFrames - avail) * out_channels * sizeof(float));
            atomic_fetch_add_explicit(&slot->underruns, 1, memory_order_relaxed);
        }
    }

    return noErr;
}

/* ──────────────────── Output Slot Setup / Teardown ──────────────────────── */

static void teardown_output(OutputSlot *slot)
{
    if (slot->output_au) {
        AudioOutputUnitStop(slot->output_au);
        AudioComponentInstanceDispose(slot->output_au);
        slot->output_au = NULL;
    }
    if (slot->converter) {
        AudioConverterDispose(slot->converter);
        slot->converter = NULL;
    }
    ring_reset(&slot->ring);
    slot->active = false;
}

static bool setup_output(OutputSlot *slot)
{
    OSStatus err;

    /* Query device properties */
    if (!get_device_sample_rate(slot->device_id, &slot->device_rate)) {
        LOG_ERROR("[%s] failed to get sample rate", slot->name);
        return false;
    }

    if (!get_device_channel_count(slot->device_id, false, &slot->device_channels)) {
        LOG_ERROR("[%s] failed to get channel count", slot->name);
        return false;
    }
    if (slot->device_channels == 0) return false;
    if (slot->device_channels > SOURCE_CHANNELS)
        slot->device_channels = SOURCE_CHANNELS;

    slot->needs_resample = ((UInt32)slot->device_rate != SOURCE_RATE);

    LOG_INFO("[%s] opening (id=%u, %uch @ %.0f Hz)%s",
             slot->name, (unsigned)slot->device_id,
             slot->device_channels, slot->device_rate,
             slot->needs_resample ? " [resample]" : "");

    /* Create converter if resampling needed */
    if (slot->needs_resample) {
        AudioStreamBasicDescription in_fmt = {
            .mSampleRate       = SOURCE_RATE,
            .mFormatID         = kAudioFormatLinearPCM,
            .mFormatFlags      = kAudioFormatFlagIsFloat | kAudioFormatFlagIsPacked,
            .mBytesPerPacket   = SOURCE_CHANNELS * sizeof(float),
            .mFramesPerPacket  = 1,
            .mBytesPerFrame    = SOURCE_CHANNELS * sizeof(float),
            .mChannelsPerFrame = SOURCE_CHANNELS,
            .mBitsPerChannel   = 32,
        };
        AudioStreamBasicDescription out_fmt = {
            .mSampleRate       = slot->device_rate,
            .mFormatID         = kAudioFormatLinearPCM,
            .mFormatFlags      = kAudioFormatFlagIsFloat | kAudioFormatFlagIsPacked,
            .mBytesPerPacket   = slot->device_channels * sizeof(float),
            .mFramesPerPacket  = 1,
            .mBytesPerFrame    = slot->device_channels * sizeof(float),
            .mChannelsPerFrame = slot->device_channels,
            .mBitsPerChannel   = 32,
        };

        err = AudioConverterNew(&in_fmt, &out_fmt, &slot->converter);
        if (err != noErr) {
            LOG_ERROR("[%s] AudioConverterNew failed: %d", slot->name, (int)err);
            return false;
        }

        UInt32 quality = kAudioConverterQuality_Medium;
        AudioConverterSetProperty(slot->converter,
            kAudioConverterSampleRateConverterQuality,
            sizeof(quality), &quality);
    }

    /* Create output AUHAL */
    AudioComponentDescription au_desc = {
        .componentType         = kAudioUnitType_Output,
        .componentSubType      = kAudioUnitSubType_HALOutput,
        .componentManufacturer = kAudioUnitManufacturer_Apple,
    };
    AudioComponent comp = AudioComponentFindNext(NULL, &au_desc);
    if (!comp) {
        LOG_ERROR("[%s] AUHAL component not found", slot->name);
        return false;
    }

    err = AudioComponentInstanceNew(comp, &slot->output_au);
    if (err != noErr) {
        LOG_ERROR("[%s] failed to create AUHAL: %d", slot->name, (int)err);
        return false;
    }

    /* Output only */
    UInt32 enable = 1, disable = 0;
    AudioUnitSetProperty(slot->output_au, kAudioOutputUnitProperty_EnableIO,
                         kAudioUnitScope_Input, 1, &disable, sizeof(disable));
    AudioUnitSetProperty(slot->output_au, kAudioOutputUnitProperty_EnableIO,
                         kAudioUnitScope_Output, 0, &enable, sizeof(enable));

    /* Set output device */
    AudioUnitSetProperty(slot->output_au, kAudioOutputUnitProperty_CurrentDevice,
                         kAudioUnitScope_Global, 0,
                         &slot->device_id, sizeof(slot->device_id));

    /* Set stream format (what we supply to the output) */
    AudioStreamBasicDescription fmt = {
        .mSampleRate       = slot->device_rate,
        .mFormatID         = kAudioFormatLinearPCM,
        .mFormatFlags      = kAudioFormatFlagIsFloat | kAudioFormatFlagIsPacked,
        .mBytesPerPacket   = slot->device_channels * sizeof(float),
        .mFramesPerPacket  = 1,
        .mBytesPerFrame    = slot->device_channels * sizeof(float),
        .mChannelsPerFrame = slot->device_channels,
        .mBitsPerChannel   = 32,
    };
    AudioUnitSetProperty(slot->output_au, kAudioUnitProperty_StreamFormat,
                         kAudioUnitScope_Input, 0, &fmt, sizeof(fmt));

    /* Set render callback */
    AURenderCallbackStruct cb = {
        .inputProc       = output_render_callback,
        .inputProcRefCon = slot,
    };
    AudioUnitSetProperty(slot->output_au, kAudioUnitProperty_SetRenderCallback,
                         kAudioUnitScope_Input, 0, &cb, sizeof(cb));

    err = AudioUnitInitialize(slot->output_au);
    if (err != noErr) {
        LOG_ERROR("[%s] AudioUnitInitialize failed: %d", slot->name, (int)err);
        teardown_output(slot);
        return false;
    }

    err = AudioOutputUnitStart(slot->output_au);
    if (err != noErr) {
        LOG_ERROR("[%s] AudioOutputUnitStart failed: %d", slot->name, (int)err);
        teardown_output(slot);
        return false;
    }

    slot->active = true;
    LOG_INFO("[%s] active", slot->name);
    return true;
}

/* ──────────────────── Input Setup / Teardown ────────────────────────────── */

static void teardown_input(void)
{
    if (g_input_au) {
        AudioOutputUnitStop(g_input_au);
        AudioComponentInstanceDispose(g_input_au);
        g_input_au = NULL;
    }
}

static bool setup_input(void)
{
    OSStatus err;

    g_input_device_id = find_device_by_name(g_source_device, true);
    if (g_input_device_id == kAudioObjectUnknown) {
        LOG_WARN("source '%s' not found", g_source_device);
        return false;
    }

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

    AudioUnitSetProperty(g_input_au, kAudioOutputUnitProperty_CurrentDevice,
                         kAudioUnitScope_Global, 0,
                         &g_input_device_id, sizeof(g_input_device_id));

    AudioStreamBasicDescription fmt = {
        .mSampleRate       = SOURCE_RATE,
        .mFormatID         = kAudioFormatLinearPCM,
        .mFormatFlags      = kAudioFormatFlagIsFloat | kAudioFormatFlagIsPacked,
        .mBytesPerPacket   = SOURCE_CHANNELS * sizeof(float),
        .mFramesPerPacket  = 1,
        .mBytesPerFrame    = SOURCE_CHANNELS * sizeof(float),
        .mChannelsPerFrame = SOURCE_CHANNELS,
        .mBitsPerChannel   = 32,
    };
    AudioUnitSetProperty(g_input_au, kAudioUnitProperty_StreamFormat,
                         kAudioUnitScope_Output, 1, &fmt, sizeof(fmt));

    AURenderCallbackStruct cb = {
        .inputProc       = input_render_callback,
        .inputProcRefCon = NULL,
    };
    AudioUnitSetProperty(g_input_au, kAudioOutputUnitProperty_SetInputCallback,
                         kAudioUnitScope_Global, 0, &cb, sizeof(cb));

    err = AudioUnitInitialize(g_input_au);
    if (err != noErr) {
        LOG_ERROR("AudioUnitInitialize (input) failed: %d", (int)err);
        teardown_input();
        return false;
    }

    err = AudioOutputUnitStart(g_input_au);
    if (err != noErr) {
        LOG_ERROR("AudioOutputUnitStart (input) failed: %d", (int)err);
        teardown_input();
        return false;
    }

    LOG_INFO("input active: %s (%dch @ %d Hz)", g_source_device, SOURCE_CHANNELS, SOURCE_RATE);
    return true;
}

/* ──────────────────── Hot-Plug Listener ──────────────────────────────────── */

static volatile sig_atomic_t g_device_changed = 0;

static OSStatus device_list_changed(
    AudioObjectID inObjectID, UInt32 inNumberAddresses,
    const AudioObjectPropertyAddress inAddresses[], void *inClientData)
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

/* ──────────────── Device Scan & Output Management ───────────────────────── */

static void scan_and_update_outputs(void)
{
    AudioDeviceID dev_ids[MAX_OUTPUTS];
    char dev_names[MAX_OUTPUTS][256];
    int n = discover_output_devices(dev_ids, dev_names, MAX_OUTPUTS);

    pthread_mutex_lock(&g_outputs_lock);

    /* Teardown outputs whose devices have disappeared */
    for (int i = 0; i < g_output_count; i++) {
        if (!g_outputs[i].active) continue;
        bool still_present = false;
        for (int j = 0; j < n; j++) {
            if (dev_ids[j] == g_outputs[i].device_id) {
                still_present = true;
                break;
            }
        }
        if (!still_present) {
            LOG_INFO("[%s] device disappeared", g_outputs[i].name);
            teardown_output(&g_outputs[i]);
        }
    }

    /* Add new outputs */
    for (int j = 0; j < n; j++) {
        /* Check if already tracked */
        bool found = false;
        for (int i = 0; i < g_output_count; i++) {
            if (g_outputs[i].device_id == dev_ids[j] && g_outputs[i].active) {
                found = true;
                break;
            }
        }
        if (found) continue;

        /* Find a free slot */
        int slot_idx = -1;
        for (int i = 0; i < g_output_count; i++) {
            if (!g_outputs[i].active) {
                slot_idx = i;
                break;
            }
        }
        if (slot_idx < 0 && g_output_count < MAX_OUTPUTS) {
            slot_idx = g_output_count++;
        }
        if (slot_idx < 0) continue; /* all slots full */

        OutputSlot *slot = &g_outputs[slot_idx];
        memset(slot, 0, sizeof(*slot));
        strncpy(slot->name, dev_names[j], 255);
        slot->device_id = dev_ids[j];
        ring_init(&slot->ring, RING_BUF_FRAMES);

        if (!setup_output(slot)) {
            ring_free(&slot->ring);
            memset(slot, 0, sizeof(*slot));
        }
    }

    pthread_mutex_unlock(&g_outputs_lock);
}

/* ──────────────────────────── Main Loop ─────────────────────────────────── */

static void main_loop(void)
{
    bool input_active = false;
    int scan_countdown = 0;

    while (!g_shutdown) {
        /* Setup input if needed */
        if (!input_active) {
            input_active = setup_input();
            if (!input_active) {
                for (int i = 0; i < 20 && !g_shutdown; i++)
                    usleep(100000);
                continue;
            }
        }

        /* Scan for outputs on startup and periodically */
        if (scan_countdown <= 0 || g_device_changed) {
            g_device_changed = 0;
            scan_and_update_outputs();
            scan_countdown = DEVICE_SCAN_INTERVAL_S * 5; /* 5 iterations/sec */
        }

        /* Sleep 200ms */
        for (int i = 0; i < 2 && !g_shutdown && !g_device_changed; i++)
            usleep(100000);
        scan_countdown--;
    }
}

/* ──────────────────────────── Entry Point ───────────────────────────────── */

int main(int argc, char *argv[])
{
    (void)argc; (void)argv;

    g_source_device = DEFAULT_SOURCE_DEVICE;
    const char *env_src = getenv("AUDIO_ASSIST_AUDIO_FORWARD_SOURCE_DEVICE");
    if (env_src && env_src[0]) g_source_device = env_src;

    LOG_INFO("audio-forward (C) starting");
    LOG_INFO("  source: %s (%dch @ %d Hz)", g_source_device, SOURCE_CHANNELS, SOURCE_RATE);

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
    LOG_INFO("audio-forward shutting down");
    teardown_input();
    pthread_mutex_lock(&g_outputs_lock);
    for (int i = 0; i < g_output_count; i++) {
        if (g_outputs[i].active) {
            teardown_output(&g_outputs[i]);
            ring_free(&g_outputs[i].ring);
        }
    }
    pthread_mutex_unlock(&g_outputs_lock);
    remove_hotplug_listener();
    LOG_INFO("audio-forward exited");

    return 0;
}
