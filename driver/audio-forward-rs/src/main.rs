#![allow(unsafe_op_in_unsafe_fn)]
#![allow(static_mut_refs)]
//! audio-forward — Low-latency audio forwarding daemon
//!
//! Forwards audio from CaptureAudio 2ch virtual device to all connected
//! physical output devices (MacBook Pro Speakers, Bose QC45, etc.) using
//! Core Audio AUHAL AudioUnits with callback-based IO.
//!
//! Architecture:
//!   CaptureAudio 2ch → Input AUHAL render callback
//!     → Lock-free SPSC ring buffer (per output device)
//!     → Per-output AUHAL render callback
//!       → AudioConverterRef if resample needed (48kHz → 44.1kHz for BT)
//!       → Physical speaker/headphone

use coreaudio_sys::*;
use std::ffi::CStr;
use std::os::raw::c_void;
use std::ptr;
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};

// ─────────────────────────── Configuration ───────────────────────────────

const DEFAULT_SOURCE_DEVICE: &str = "CaptureAudio 2ch";
const SOURCE_RATE: u32 = 48000;
const SOURCE_CHANNELS: u32 = 2;
const RING_BUF_FRAMES: u32 = 8192; // ~170ms at 48kHz, power of 2
const MAX_OUTPUTS: usize = 8;
const DEVICE_SCAN_INTERVAL_TICKS: i32 = 25; // 25 * 200ms = 5s
const MAX_CALLBACK_FRAMES: usize = 4096;
const PREFERRED_IO_FRAMES: u32 = 256; // request ~5.3ms IO buffers from CoreAudio
const MAX_RING_LATENCY_FRAMES: u32 = 1024; // ~21ms — skip ahead if ring accumulates more

const EXCLUDED: &[&str] = &[
    "capturemic",
    "captureaudio",
    "blackhole",
    "aggregate device",
    "multi-output device",
    "zoomaudiodevice",
    "cluely",
    "microsoft teams audio",
];

// ─────────────────────────── Logging ─────────────────────────────────────

fn log_msg(level: &str, msg: &str) {
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default();
    let t = now.as_secs() as i64;
    let mut tm = libc_ffi::Tm::default();
    unsafe { libc_ffi::localtime_r(&t, &mut tm) };

    eprintln!(
        "{:04}-{:02}-{:02} {:02}:{:02}:{:02} [{}] {}",
        tm.tm_year + 1900,
        tm.tm_mon + 1,
        tm.tm_mday,
        tm.tm_hour,
        tm.tm_min,
        tm.tm_sec,
        level,
        msg
    );
}

macro_rules! log_info {
    ($($arg:tt)*) => { log_msg("INFO", &format!($($arg)*)) };
}

macro_rules! log_warn {
    ($($arg:tt)*) => { log_msg("WARNING", &format!($($arg)*)) };
}

macro_rules! log_error {
    ($($arg:tt)*) => { log_msg("ERROR", &format!($($arg)*)) };
}

// ─────────────────────── Lock-free Ring Buffer ───────────────────────────

struct RingBuffer {
    buffer: Vec<f32>,
    capacity: u32,
    mask: u32,
    write_pos: AtomicU32,
    read_pos: AtomicU32,
}

impl RingBuffer {
    fn new(frame_capacity: u32) -> Self {
        Self {
            buffer: vec![0.0f32; (frame_capacity * SOURCE_CHANNELS) as usize],
            capacity: frame_capacity,
            mask: frame_capacity - 1,
            write_pos: AtomicU32::new(0),
            read_pos: AtomicU32::new(0),
        }
    }

    fn reset(&self) {
        self.write_pos.store(0, Ordering::Release);
        self.read_pos.store(0, Ordering::Release);
    }

    fn available_read(&self) -> u32 {
        let w = self.write_pos.load(Ordering::Acquire);
        let r = self.read_pos.load(Ordering::Relaxed);
        w.wrapping_sub(r)
    }

    fn available_write(&self) -> u32 {
        self.capacity - self.available_read()
    }

    /// Advance read position by `frames` without copying data (discard).
    fn skip_read(&self, frames: u32) {
        let pos = self.read_pos.load(Ordering::Relaxed);
        self.read_pos
            .store(pos.wrapping_add(frames), Ordering::Release);
    }

    /// # Safety
    /// Only one writer thread may call this. `data` must point to
    /// at least `frames * SOURCE_CHANNELS` floats.
    unsafe fn write(&self, data: *const f32, frames: u32) {
        let pos = self.write_pos.load(Ordering::Relaxed);
        let buf = self.buffer.as_ptr() as *mut f32;
        for i in 0..frames {
            let idx = ((pos + i) & self.mask) as usize;
            *buf.add(idx * 2) = *data.add(i as usize * 2);
            *buf.add(idx * 2 + 1) = *data.add(i as usize * 2 + 1);
        }
        self.write_pos
            .store(pos.wrapping_add(frames), Ordering::Release);
    }

    /// # Safety
    /// Only one reader thread may call this. `data` must point to
    /// at least `frames * out_channels` floats.
    unsafe fn read(&self, data: *mut f32, frames: u32, out_channels: u32) {
        let pos = self.read_pos.load(Ordering::Relaxed);
        for i in 0..frames {
            let idx = ((pos + i) & self.mask) as usize;
            if out_channels == 2 {
                *data.add(i as usize * 2) = self.buffer[idx * 2];
                *data.add(i as usize * 2 + 1) = self.buffer[idx * 2 + 1];
            } else {
                *data.add(i as usize) =
                    (self.buffer[idx * 2] + self.buffer[idx * 2 + 1]) * 0.5;
            }
        }
        self.read_pos
            .store(pos.wrapping_add(frames), Ordering::Release);
    }
}

unsafe impl Send for RingBuffer {}
unsafe impl Sync for RingBuffer {}

// ──────────────────────── Per-Output State ────────────────────────────────

struct OutputSlot {
    name: String,
    device_id: AudioDeviceID,
    output_au: AudioComponentInstance,
    converter: AudioConverterRef,
    ring: Box<RingBuffer>,
    device_rate: f64,
    device_channels: u32,
    needs_resample: bool,
    active: bool,
    underruns: AtomicU32,
}

#[repr(C)]
struct ConverterState {
    src: *const f32,
    src_frames: u32,
    src_pos: u32,
    src_channels: u32,
}

// ──────────────────────── Global State ────────────────────────────────────

static SHUTDOWN: AtomicBool = AtomicBool::new(false);
static DEVICE_CHANGED: AtomicBool = AtomicBool::new(false);

static mut G_INPUT_AU: AudioComponentInstance = ptr::null_mut();
static mut G_OUTPUTS: *mut Vec<OutputSlot> = ptr::null_mut();
static mut G_OUTPUT_COUNT: usize = 0;

// ──────────────────────── Device Discovery ────────────────────────────────

fn is_excluded(name: &str) -> bool {
    let lower = name.to_lowercase();
    EXCLUDED.iter().any(|ex| lower.contains(ex))
}

fn get_device_name(device_id: AudioDeviceID) -> Option<String> {
    unsafe {
        let mut name_ref: CFStringRef = ptr::null();
        let mut size = std::mem::size_of::<CFStringRef>() as u32;
        let addr = AudioObjectPropertyAddress {
            mSelector: kAudioObjectPropertyName,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain,
        };
        let err = AudioObjectGetPropertyData(
            device_id, &addr, 0, ptr::null(),
            &mut size, &mut name_ref as *mut _ as *mut c_void,
        );
        if err != 0 || name_ref.is_null() {
            return None;
        }
        let len = CFStringGetLength(name_ref);
        let mut buf = vec![0u8; (len as usize + 1) * 4];
        let ok = CFStringGetCString(
            name_ref, buf.as_mut_ptr() as *mut i8,
            buf.len() as i64, kCFStringEncodingUTF8,
        );
        CFRelease(name_ref as *const c_void);
        if ok == 0 { return None; }
        CStr::from_ptr(buf.as_ptr() as *const i8)
            .to_str().ok().map(|s| s.to_string())
    }
}

fn find_device_by_name(name: &str, is_input: bool) -> Option<AudioDeviceID> {
    unsafe {
        let addr = AudioObjectPropertyAddress {
            mSelector: kAudioHardwarePropertyDevices,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain,
        };
        let mut size: u32 = 0;
        if AudioObjectGetPropertyDataSize(
            kAudioObjectSystemObject, &addr, 0, ptr::null(), &mut size,
        ) != 0 || size == 0 {
            return None;
        }
        let count = size as usize / std::mem::size_of::<AudioDeviceID>();
        let mut devices = vec![0u32; count];
        if AudioObjectGetPropertyData(
            kAudioObjectSystemObject, &addr, 0, ptr::null(),
            &mut size, devices.as_mut_ptr() as *mut c_void,
        ) != 0 {
            return None;
        }
        let name_lower = name.to_lowercase();
        for &dev_id in &devices {
            let Some(dev_name) = get_device_name(dev_id) else { continue };
            if !dev_name.to_lowercase().contains(&name_lower) { continue; }
            let scope = if is_input {
                kAudioDevicePropertyScopeInput
            } else {
                kAudioDevicePropertyScopeOutput
            };
            let stream_addr = AudioObjectPropertyAddress {
                mSelector: kAudioDevicePropertyStreams,
                mScope: scope,
                mElement: kAudioObjectPropertyElementMain,
            };
            let mut stream_size: u32 = 0;
            if AudioObjectGetPropertyDataSize(
                dev_id, &stream_addr, 0, ptr::null(), &mut stream_size,
            ) != 0 || stream_size == 0 {
                continue;
            }
            return Some(dev_id);
        }
        None
    }
}

fn get_device_sample_rate(device_id: AudioDeviceID) -> Option<f64> {
    unsafe {
        let addr = AudioObjectPropertyAddress {
            mSelector: kAudioDevicePropertyNominalSampleRate,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain,
        };
        let mut rate: f64 = 0.0;
        let mut size = std::mem::size_of::<f64>() as u32;
        if AudioObjectGetPropertyData(
            device_id, &addr, 0, ptr::null(),
            &mut size, &mut rate as *mut f64 as *mut c_void,
        ) != 0 {
            None
        } else {
            Some(rate)
        }
    }
}

fn get_device_channel_count(device_id: AudioDeviceID, is_input: bool) -> Option<u32> {
    unsafe {
        let scope = if is_input { kAudioDevicePropertyScopeInput } else { kAudioDevicePropertyScopeOutput };
        let addr = AudioObjectPropertyAddress {
            mSelector: kAudioDevicePropertyStreamConfiguration,
            mScope: scope,
            mElement: kAudioObjectPropertyElementMain,
        };
        let mut size: u32 = 0;
        if AudioObjectGetPropertyDataSize(device_id, &addr, 0, ptr::null(), &mut size) != 0 {
            return None;
        }
        let layout = std::alloc::Layout::from_size_align(size as usize, 8).ok()?;
        let raw = std::alloc::alloc(layout);
        if raw.is_null() { return None; }
        if AudioObjectGetPropertyData(
            device_id, &addr, 0, ptr::null(), &mut size, raw as *mut c_void,
        ) != 0 {
            std::alloc::dealloc(raw, layout);
            return None;
        }
        let abl = raw as *const AudioBufferList;
        let n = (*abl).mNumberBuffers;
        let bufs = &(*abl).mBuffers as *const AudioBuffer;
        let mut total = 0u32;
        for i in 0..n { total += (*bufs.add(i as usize)).mNumberChannels; }
        std::alloc::dealloc(raw, layout);
        Some(total)
    }
}

fn discover_output_devices() -> Vec<(AudioDeviceID, String)> {
    unsafe {
        let addr = AudioObjectPropertyAddress {
            mSelector: kAudioHardwarePropertyDevices,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain,
        };
        let mut size: u32 = 0;
        if AudioObjectGetPropertyDataSize(
            kAudioObjectSystemObject, &addr, 0, ptr::null(), &mut size,
        ) != 0 || size == 0 {
            return vec![];
        }
        let count = size as usize / std::mem::size_of::<AudioDeviceID>();
        let mut devices = vec![0u32; count];
        if AudioObjectGetPropertyData(
            kAudioObjectSystemObject, &addr, 0, ptr::null(),
            &mut size, devices.as_mut_ptr() as *mut c_void,
        ) != 0 {
            return vec![];
        }
        let mut result = Vec::new();
        for &dev_id in &devices {
            let Some(ch) = get_device_channel_count(dev_id, false) else { continue };
            if ch == 0 { continue; }
            let Some(name) = get_device_name(dev_id) else { continue };
            if is_excluded(&name) { continue; }
            result.push((dev_id, name));
        }
        result
    }
}

// ──────────── Callbacks ──────────────────────────────────────────────────

unsafe extern "C" fn converter_input_cb(
    _converter: AudioConverterRef,
    io_packets: *mut u32,
    io_data: *mut AudioBufferList,
    _out_desc: *mut *mut AudioStreamPacketDescription,
    user_data: *mut c_void,
) -> i32 {
    let state = &mut *(user_data as *mut ConverterState);
    let remaining = state.src_frames - state.src_pos;
    if remaining == 0 { *io_packets = 0; return 0; }
    let frames = (*io_packets).min(remaining);
    (*io_data).mNumberBuffers = 1;
    (*io_data).mBuffers[0].mNumberChannels = state.src_channels;
    (*io_data).mBuffers[0].mDataByteSize = frames * state.src_channels * 4;
    (*io_data).mBuffers[0].mData =
        state.src.add((state.src_pos * state.src_channels) as usize) as *mut c_void;
    state.src_pos += frames;
    *io_packets = frames;
    0
}

unsafe extern "C" fn input_render_cb(
    _ref_con: *mut c_void,
    action_flags: *mut u32,
    timestamp: *const AudioTimeStamp,
    bus: u32,
    n_frames: u32,
    _io_data: *mut AudioBufferList,
) -> i32 {
    if SHUTDOWN.load(Ordering::Relaxed) { return 0; }
    let mut raw_buf = [0.0f32; MAX_CALLBACK_FRAMES * SOURCE_CHANNELS as usize];
    let mut abl = AudioBufferList {
        mNumberBuffers: 1,
        mBuffers: [AudioBuffer {
            mNumberChannels: SOURCE_CHANNELS,
            mDataByteSize: n_frames * SOURCE_CHANNELS * 4,
            mData: raw_buf.as_mut_ptr() as *mut c_void,
        }],
    };
    let err = AudioUnitRender(G_INPUT_AU, action_flags, timestamp, bus, n_frames, &mut abl);
    if err != 0 { return err; }
    let frames = n_frames.min(MAX_CALLBACK_FRAMES as u32);

    if !G_OUTPUTS.is_null() {
        let outputs = &*G_OUTPUTS;
        for slot in outputs.iter().take(G_OUTPUT_COUNT) {
            if !slot.active { continue; }
            if slot.ring.available_write() >= frames {
                slot.ring.write(raw_buf.as_ptr(), frames);
            }
        }
    }
    0
}

unsafe extern "C" fn output_render_cb(
    ref_con: *mut c_void,
    _action_flags: *mut u32,
    _timestamp: *const AudioTimeStamp,
    _bus: u32,
    n_frames: u32,
    io_data: *mut AudioBufferList,
) -> i32 {
    let slot = &*(ref_con as *const OutputSlot);
    if io_data.is_null() || (*io_data).mNumberBuffers == 0 { return 0; }
    let out = (*io_data).mBuffers[0].mData as *mut f32;
    let out_ch = slot.device_channels;

    // Latency control: if ring buffer accumulated more than MAX_RING_LATENCY_FRAMES
    // ahead of what we need, skip forward to stay close to real-time
    let buffered = slot.ring.available_read();
    if buffered > MAX_RING_LATENCY_FRAMES + n_frames {
        let skip = buffered - MAX_RING_LATENCY_FRAMES;
        slot.ring.skip_read(skip);
    }

    if slot.needs_resample && !slot.converter.is_null() {
        let ratio = SOURCE_RATE as f64 / slot.device_rate;
        let src_needed = ((n_frames as f64 * ratio) as u32 + 1).min(MAX_CALLBACK_FRAMES as u32);
        let avail = slot.ring.available_read();
        if avail < src_needed {
            ptr::write_bytes(out, 0, (n_frames * out_ch) as usize);
            slot.underruns.fetch_add(1, Ordering::Relaxed);
            return 0;
        }
        let mut src_buf = [0.0f32; MAX_CALLBACK_FRAMES * SOURCE_CHANNELS as usize];
        slot.ring.read(src_buf.as_mut_ptr(), src_needed, SOURCE_CHANNELS);

        let mut conv_state = ConverterState {
            src: src_buf.as_ptr(), src_frames: src_needed,
            src_pos: 0, src_channels: SOURCE_CHANNELS,
        };
        let mut out_abl = AudioBufferList {
            mNumberBuffers: 1,
            mBuffers: [AudioBuffer {
                mNumberChannels: out_ch,
                mDataByteSize: n_frames * out_ch * 4,
                mData: out as *mut c_void,
            }],
        };
        let mut out_packets = n_frames;
        let cerr = AudioConverterFillComplexBuffer(
            slot.converter, Some(converter_input_cb),
            &mut conv_state as *mut _ as *mut c_void,
            &mut out_packets, &mut out_abl, ptr::null_mut(),
        );
        if cerr != 0 && cerr != 1 {
            ptr::write_bytes(out, 0, (n_frames * out_ch) as usize);
        }
    } else {
        let avail = slot.ring.available_read();
        if avail >= n_frames {
            slot.ring.read(out, n_frames, out_ch);
        } else {
            if avail > 0 { slot.ring.read(out, avail, out_ch); }
            ptr::write_bytes(
                out.add((avail * out_ch) as usize), 0,
                ((n_frames - avail) * out_ch) as usize,
            );
            slot.underruns.fetch_add(1, Ordering::Relaxed);
        }
    }
    0
}

// ──────────────────── Setup / Teardown ────────────────────────────────────

fn make_pcm_format(rate: f64, channels: u32) -> AudioStreamBasicDescription {
    AudioStreamBasicDescription {
        mSampleRate: rate,
        mFormatID: kAudioFormatLinearPCM,
        mFormatFlags: kAudioFormatFlagIsFloat | kAudioFormatFlagIsPacked,
        mBytesPerPacket: channels * 4,
        mFramesPerPacket: 1,
        mBytesPerFrame: channels * 4,
        mChannelsPerFrame: channels,
        mBitsPerChannel: 32,
        mReserved: 0,
    }
}

fn teardown_output(slot: &mut OutputSlot) {
    unsafe {
        if !slot.output_au.is_null() {
            AudioOutputUnitStop(slot.output_au);
            AudioComponentInstanceDispose(slot.output_au);
            slot.output_au = ptr::null_mut();
        }
        if !slot.converter.is_null() {
            AudioConverterDispose(slot.converter);
            slot.converter = ptr::null_mut();
        }
    }
    slot.ring.reset();
    slot.active = false;
}

fn setup_output(slot: &mut OutputSlot) -> bool {
    let Some(rate) = get_device_sample_rate(slot.device_id) else {
        log_error!("[{}] failed to get sample rate", slot.name);
        return false;
    };
    slot.device_rate = rate;
    let Some(ch) = get_device_channel_count(slot.device_id, false) else {
        log_error!("[{}] failed to get channel count", slot.name);
        return false;
    };
    if ch == 0 { return false; }
    slot.device_channels = ch.min(SOURCE_CHANNELS);
    slot.needs_resample = slot.device_rate as u32 != SOURCE_RATE;

    log_info!("[{}] opening (id={}, {}ch @ {} Hz){}",
        slot.name, slot.device_id, slot.device_channels,
        slot.device_rate as u32,
        if slot.needs_resample { " [resample]" } else { "" });

    unsafe {
        if slot.needs_resample {
            let in_fmt = make_pcm_format(SOURCE_RATE as f64, SOURCE_CHANNELS);
            let out_fmt = make_pcm_format(slot.device_rate, slot.device_channels);
            if AudioConverterNew(&in_fmt, &out_fmt, &mut slot.converter) != 0 {
                log_error!("[{}] AudioConverterNew failed", slot.name);
                return false;
            }
            let quality = kAudioConverterQuality_Medium;
            AudioConverterSetProperty(slot.converter,
                kAudioConverterSampleRateConverterQuality,
                4, &quality as *const u32 as *const c_void);
        }

        let au_desc = AudioComponentDescription {
            componentType: kAudioUnitType_Output,
            componentSubType: kAudioUnitSubType_HALOutput,
            componentManufacturer: kAudioUnitManufacturer_Apple,
            componentFlags: 0, componentFlagsMask: 0,
        };
        let comp = AudioComponentFindNext(ptr::null_mut(), &au_desc);
        if comp.is_null() { log_error!("[{}] AUHAL not found", slot.name); return false; }
        if AudioComponentInstanceNew(comp, &mut slot.output_au) != 0 {
            log_error!("[{}] failed to create AUHAL", slot.name); return false;
        }

        let enable: u32 = 1; let disable: u32 = 0;
        AudioUnitSetProperty(slot.output_au, kAudioOutputUnitProperty_EnableIO,
            kAudioUnitScope_Input, 1, &disable as *const _ as *const c_void, 4);
        AudioUnitSetProperty(slot.output_au, kAudioOutputUnitProperty_EnableIO,
            kAudioUnitScope_Output, 0, &enable as *const _ as *const c_void, 4);
        AudioUnitSetProperty(slot.output_au, kAudioOutputUnitProperty_CurrentDevice,
            kAudioUnitScope_Global, 0, &slot.device_id as *const _ as *const c_void,
            std::mem::size_of::<AudioDeviceID>() as u32);

        // Request small IO buffer for lower latency
        let buf_frames = PREFERRED_IO_FRAMES;
        let buf_addr = AudioObjectPropertyAddress {
            mSelector: kAudioDevicePropertyBufferFrameSize,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain,
        };
        AudioObjectSetPropertyData(
            slot.device_id, &buf_addr, 0, ptr::null(),
            4, &buf_frames as *const u32 as *const c_void);

        let fmt = make_pcm_format(slot.device_rate, slot.device_channels);
        AudioUnitSetProperty(slot.output_au, kAudioUnitProperty_StreamFormat,
            kAudioUnitScope_Input, 0, &fmt as *const _ as *const c_void,
            std::mem::size_of::<AudioStreamBasicDescription>() as u32);

        let cb = AURenderCallbackStruct {
            inputProc: Some(output_render_cb),
            inputProcRefCon: slot as *mut OutputSlot as *mut c_void,
        };
        AudioUnitSetProperty(slot.output_au, kAudioUnitProperty_SetRenderCallback,
            kAudioUnitScope_Input, 0, &cb as *const _ as *const c_void,
            std::mem::size_of::<AURenderCallbackStruct>() as u32);

        if AudioUnitInitialize(slot.output_au) != 0 {
            log_error!("[{}] AudioUnitInitialize failed", slot.name);
            teardown_output(slot); return false;
        }
        if AudioOutputUnitStart(slot.output_au) != 0 {
            log_error!("[{}] AudioOutputUnitStart failed", slot.name);
            teardown_output(slot); return false;
        }
    }
    slot.active = true;
    log_info!("[{}] active", slot.name);
    true
}

fn teardown_input() {
    unsafe {
        if !G_INPUT_AU.is_null() {
            AudioOutputUnitStop(G_INPUT_AU);
            AudioComponentInstanceDispose(G_INPUT_AU);
            G_INPUT_AU = ptr::null_mut();
        }
    }
}

fn setup_input(source_device: &str) -> bool {
    let Some(device_id) = find_device_by_name(source_device, true) else {
        log_warn!("source '{}' not found", source_device);
        return false;
    };
    unsafe {
        let au_desc = AudioComponentDescription {
            componentType: kAudioUnitType_Output,
            componentSubType: kAudioUnitSubType_HALOutput,
            componentManufacturer: kAudioUnitManufacturer_Apple,
            componentFlags: 0, componentFlagsMask: 0,
        };
        let comp = AudioComponentFindNext(ptr::null_mut(), &au_desc);
        if comp.is_null() { log_error!("AUHAL not found"); return false; }
        if AudioComponentInstanceNew(comp, &mut G_INPUT_AU) != 0 {
            log_error!("failed to create input AUHAL"); return false;
        }

        let enable: u32 = 1; let disable: u32 = 0;
        AudioUnitSetProperty(G_INPUT_AU, kAudioOutputUnitProperty_EnableIO,
            kAudioUnitScope_Input, 1, &enable as *const _ as *const c_void, 4);
        AudioUnitSetProperty(G_INPUT_AU, kAudioOutputUnitProperty_EnableIO,
            kAudioUnitScope_Output, 0, &disable as *const _ as *const c_void, 4);
        AudioUnitSetProperty(G_INPUT_AU, kAudioOutputUnitProperty_CurrentDevice,
            kAudioUnitScope_Global, 0, &device_id as *const _ as *const c_void,
            std::mem::size_of::<AudioDeviceID>() as u32);

        // Request small IO buffer for lower latency
        let buf_frames = PREFERRED_IO_FRAMES;
        let buf_addr = AudioObjectPropertyAddress {
            mSelector: kAudioDevicePropertyBufferFrameSize,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain,
        };
        AudioObjectSetPropertyData(
            device_id, &buf_addr, 0, ptr::null(),
            4, &buf_frames as *const u32 as *const c_void);

        let fmt = make_pcm_format(SOURCE_RATE as f64, SOURCE_CHANNELS);
        AudioUnitSetProperty(G_INPUT_AU, kAudioUnitProperty_StreamFormat,
            kAudioUnitScope_Output, 1, &fmt as *const _ as *const c_void,
            std::mem::size_of::<AudioStreamBasicDescription>() as u32);

        let cb = AURenderCallbackStruct {
            inputProc: Some(input_render_cb),
            inputProcRefCon: ptr::null_mut(),
        };
        AudioUnitSetProperty(G_INPUT_AU, kAudioOutputUnitProperty_SetInputCallback,
            kAudioUnitScope_Global, 0, &cb as *const _ as *const c_void,
            std::mem::size_of::<AURenderCallbackStruct>() as u32);

        if AudioUnitInitialize(G_INPUT_AU) != 0 {
            log_error!("AudioUnitInitialize (input) failed");
            teardown_input(); return false;
        }
        if AudioOutputUnitStart(G_INPUT_AU) != 0 {
            log_error!("AudioOutputUnitStart (input) failed");
            teardown_input(); return false;
        }
    }
    log_info!("input active: {} ({}ch @ {} Hz)", source_device, SOURCE_CHANNELS, SOURCE_RATE);
    true
}

// ──────────────────── Hot-Plug Listener ──────────────────────────────────

unsafe extern "C" fn device_list_changed(
    _id: AudioObjectID, _num: u32,
    _addrs: *const AudioObjectPropertyAddress, _data: *mut c_void,
) -> i32 {
    DEVICE_CHANGED.store(true, Ordering::Release);
    0
}

fn install_hotplug_listener() {
    unsafe {
        let addr = AudioObjectPropertyAddress {
            mSelector: kAudioHardwarePropertyDevices,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain,
        };
        AudioObjectAddPropertyListener(
            kAudioObjectSystemObject, &addr, Some(device_list_changed), ptr::null_mut());
    }
}

fn remove_hotplug_listener() {
    unsafe {
        let addr = AudioObjectPropertyAddress {
            mSelector: kAudioHardwarePropertyDevices,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain,
        };
        AudioObjectRemovePropertyListener(
            kAudioObjectSystemObject, &addr, Some(device_list_changed), ptr::null_mut());
    }
}

// ──────────────── Device Scan & Output Management ───────────────────────

fn scan_and_update_outputs(outputs: &mut Vec<OutputSlot>) {
    let discovered = discover_output_devices();

    for slot in outputs.iter_mut() {
        if !slot.active { continue; }
        if !discovered.iter().any(|(id, _)| *id == slot.device_id) {
            log_info!("[{}] device disappeared", slot.name);
            teardown_output(slot);
        }
    }

    for (dev_id, dev_name) in &discovered {
        if outputs.iter().any(|s| s.device_id == *dev_id && s.active) { continue; }

        let reuse_idx = if outputs.len() >= MAX_OUTPUTS {
            outputs.iter().position(|s| !s.active)
        } else {
            None
        };

        let idx = if let Some(i) = reuse_idx {
            i
        } else if outputs.len() < MAX_OUTPUTS {
            outputs.push(OutputSlot {
                name: String::new(), device_id: 0, output_au: ptr::null_mut(),
                converter: ptr::null_mut(), ring: Box::new(RingBuffer::new(RING_BUF_FRAMES)),
                device_rate: 0.0, device_channels: 0, needs_resample: false,
                active: false, underruns: AtomicU32::new(0),
            });
            outputs.len() - 1
        } else {
            continue;
        };

        outputs[idx].name = dev_name.clone();
        outputs[idx].device_id = *dev_id;
        outputs[idx].ring = Box::new(RingBuffer::new(RING_BUF_FRAMES));
        outputs[idx].underruns = AtomicU32::new(0);

        if !setup_output(&mut outputs[idx]) {
            outputs[idx].active = false;
        }
    }

    unsafe { G_OUTPUT_COUNT = outputs.len(); }
}

// ──────────────────────────── Main ───────────────────────────────────────

unsafe extern "C" fn sig_handler(_sig: i32) {
    SHUTDOWN.store(true, Ordering::Release);
}

fn main() {
    let source_device = std::env::var("AUDIO_ASSIST_AUDIO_FORWARD_SOURCE_DEVICE")
        .unwrap_or_else(|_| DEFAULT_SOURCE_DEVICE.to_string());

    log_info!("audio-forward (Rust) starting");
    log_info!("  source: {} ({}ch @ {} Hz)", source_device, SOURCE_CHANNELS, SOURCE_RATE);

    unsafe {
        libc_ffi::signal(libc_ffi::SIGTERM, sig_handler as usize);
        libc_ffi::signal(libc_ffi::SIGINT, sig_handler as usize);
    }

    let mut outputs: Vec<OutputSlot> = Vec::new();
    unsafe { G_OUTPUTS = &mut outputs as *mut Vec<OutputSlot>; }

    install_hotplug_listener();

    let mut input_active = false;
    let mut scan_countdown: i32 = 0;

    while !SHUTDOWN.load(Ordering::Relaxed) {
        if !input_active {
            input_active = setup_input(&source_device);
            if !input_active {
                for _ in 0..20 {
                    if SHUTDOWN.load(Ordering::Relaxed) { break; }
                    std::thread::sleep(std::time::Duration::from_millis(100));
                }
                continue;
            }
        }

        if scan_countdown <= 0 || DEVICE_CHANGED.load(Ordering::Relaxed) {
            DEVICE_CHANGED.store(false, Ordering::Relaxed);
            scan_and_update_outputs(&mut outputs);
            scan_countdown = DEVICE_SCAN_INTERVAL_TICKS;
        }

        for _ in 0..2 {
            if SHUTDOWN.load(Ordering::Relaxed) || DEVICE_CHANGED.load(Ordering::Relaxed) { break; }
            std::thread::sleep(std::time::Duration::from_millis(100));
        }
        scan_countdown -= 1;
    }

    log_info!("audio-forward shutting down");
    teardown_input();
    for slot in outputs.iter_mut() {
        if slot.active { teardown_output(slot); }
    }
    unsafe { G_OUTPUTS = ptr::null_mut(); }
    remove_hotplug_listener();
    log_info!("audio-forward exited");
}

// Minimal libc bindings (avoid pulling in full libc crate)
mod libc_ffi {
    use std::ptr;

    #[repr(C)]
    pub struct Tm {
        pub tm_sec: i32, pub tm_min: i32, pub tm_hour: i32,
        pub tm_mday: i32, pub tm_mon: i32, pub tm_year: i32,
        pub tm_wday: i32, pub tm_yday: i32, pub tm_isdst: i32,
        pub tm_gmtoff: i64, pub tm_zone: *const i8,
    }
    impl Default for Tm {
        fn default() -> Self {
            Self {
                tm_sec: 0, tm_min: 0, tm_hour: 0,
                tm_mday: 0, tm_mon: 0, tm_year: 0,
                tm_wday: 0, tm_yday: 0, tm_isdst: 0,
                tm_gmtoff: 0, tm_zone: ptr::null(),
            }
        }
    }
    pub const SIGTERM: i32 = 15;
    pub const SIGINT: i32 = 2;
    unsafe extern "C" {
        pub fn localtime_r(time: *const i64, result: *mut Tm) -> *mut Tm;
        pub fn signal(sig: i32, handler: usize) -> usize;
    }
}
