//! Native layer of the `timecode-tools` Python package, built with PyO3.
//!
//! This is an internal interface (`timecode_tools._native`): the public
//! Python API lives in `python/timecode_tools/` and is typed there. All
//! timecode logic (decoding, lock, jump and loss) is the Rust crate's; Python
//! adds listeners, threads, asyncio and argument checking.
//!
//! Times cross this boundary in seconds (float) on the caller's monotonic
//! clock; the core works in nanoseconds.
#![deny(clippy::all)]

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use std::time::Duration;
use timecode_tools::{
    label::{frames_per_day as day_length, Label},
    ltc::{LtcDecoder, LtcError, PcmChunk, ReadOptions, Samples},
    mtc,
    receiver::{
        LtcReceiver, MidiChunk, MtcReceiver, ReceiverEvent, ReceiverOptions, TimecodeReceiver,
    },
    synth::{generate, SynthConfig},
    timecode::{FrameRate, Timecode},
};

fn invalid(message: impl Into<String>) -> PyErr {
    PyValueError::new_err(message.into())
}

fn ltc_error(e: LtcError) -> PyErr {
    invalid(e.to_string())
}

fn rate(name: Option<&str>) -> PyResult<Option<FrameRate>> {
    match name {
        None | Some("auto") => Ok(None),
        Some(n) => FrameRate::from_name(n)
            .map(Some)
            .ok_or_else(|| invalid(format!("unknown frame rate {n:?}"))),
    }
}

fn fixed_rate(name: &str) -> PyResult<FrameRate> {
    rate(Some(name))?.ok_or_else(|| invalid("a fixed frame rate is required"))
}

fn receiver_options(
    frame_rate: Option<&str>,
    offset_frames: i64,
    lost_after: f64,
    channel: Option<u32>,
) -> PyResult<ReceiverOptions> {
    if !(lost_after.is_finite() && lost_after > 0.) {
        return Err(invalid("lost_after must be a positive number of seconds"));
    }
    let channel = match channel {
        None => None,
        Some(c) if (1..=64).contains(&c) => Some(c as u8),
        Some(c) => return Err(invalid(format!("channel must be 1..64, got {c}"))),
    };
    Ok(ReceiverOptions {
        frame_rate: rate(frame_rate)?,
        offset_frames,
        lost_after: Duration::from_secs_f64(lost_after),
        channel,
    })
}

fn s_to_ns(seconds: f64) -> PyResult<u64> {
    if seconds.is_finite() && seconds >= 0. {
        Ok((seconds * 1e9).round() as u64)
    } else {
        Err(invalid(
            "timestamp must be a non-negative number of seconds",
        ))
    }
}

/// Interleaved samples from native-endian bytes (the Python layer passes the
/// raw memory of an int16 or float32 buffer).
enum Pcm {
    I16(Vec<i16>),
    F32(Vec<f32>),
}

impl Pcm {
    fn new(data: &[u8], float: bool) -> PyResult<Self> {
        if float {
            if !data.len().is_multiple_of(4) {
                return Err(invalid("float32 data must be a multiple of 4 bytes"));
            }
            Ok(Pcm::F32(
                data.as_chunks::<4>()
                    .0
                    .iter()
                    .map(|b| f32::from_ne_bytes(*b))
                    .collect(),
            ))
        } else {
            if !data.len().is_multiple_of(2) {
                return Err(invalid("int16 data must be a multiple of 2 bytes"));
            }
            Ok(Pcm::I16(
                data.as_chunks::<2>()
                    .0
                    .iter()
                    .map(|b| i16::from_ne_bytes(*b))
                    .collect(),
            ))
        }
    }

    fn samples(&self) -> Samples<'_> {
        match self {
            Pcm::I16(v) => Samples::I16(v),
            Pcm::F32(v) => Samples::F32(v),
        }
    }
}

/// (hours, minutes, seconds, frames, drop_frame, frame_rate, locked,
/// timestamp, sample_index, channel)
type NativeTimecode = (
    u8,
    u8,
    u8,
    u8,
    bool,
    Option<&'static str>,
    bool,
    Option<f64>,
    Option<u64>,
    Option<u32>,
);

fn to_py(tc: Timecode) -> NativeTimecode {
    (
        tc.label.hours,
        tc.label.minutes,
        tc.label.seconds,
        tc.label.frames,
        tc.label.drop_frame,
        tc.frame_rate.map(|r| r.name()),
        tc.locked,
        tc.timestamp_ns.map(|ns| ns as f64 / 1e9),
        tc.sample_index,
        tc.channel,
    )
}

/// (kind, timecode, previous): `kind` is `lock`, `frame`, `jump` or `lost`;
/// for `frame`, `previous` is the index of the previous frame when playback
/// ran on.
type NativeEvent = (&'static str, Option<NativeTimecode>, Option<i64>);

fn events(list: Vec<ReceiverEvent>) -> Vec<NativeEvent> {
    list.into_iter()
        .map(|e| match e {
            ReceiverEvent::Lock(tc) => ("lock", Some(to_py(tc)), None),
            ReceiverEvent::Frame { timecode, previous } => {
                ("frame", Some(to_py(timecode)), previous)
            }
            ReceiverEvent::Jump(tc) => ("jump", Some(to_py(tc)), None),
            ReceiverEvent::Lost => ("lost", None, None),
        })
        .collect()
}

#[pyclass(module = "timecode_tools._native")]
struct NativeLtcDecoder {
    inner: LtcDecoder,
}

#[pymethods]
impl NativeLtcDecoder {
    #[new]
    fn new(
        sample_rate: u32,
        channels: u16,
        channel: u8,
        frame_rate: Option<&str>,
        offset_frames: i64,
    ) -> PyResult<Self> {
        Ok(Self {
            inner: LtcDecoder::new(
                sample_rate,
                channels,
                Some(channel),
                ReadOptions {
                    frame_rate: rate(frame_rate)?,
                    offset_frames,
                },
            )
            .map_err(ltc_error)?,
        })
    }

    #[pyo3(signature = (data, float, timestamp=None))]
    fn push(
        &mut self,
        data: &[u8],
        float: bool,
        timestamp: Option<f64>,
    ) -> PyResult<Vec<NativeTimecode>> {
        let pcm = Pcm::new(data, float)?;
        let ts = timestamp.map(s_to_ns).transpose()?;
        let out = self.inner.push(pcm.samples(), ts).map_err(ltc_error)?;
        Ok(out.into_iter().map(to_py).collect())
    }

    #[getter]
    fn locked(&self) -> bool {
        self.inner.locked()
    }
    #[getter]
    fn frame_rate(&self) -> Option<&'static str> {
        self.inner.frame_rate().map(|r| r.name())
    }
    #[getter]
    fn signal_level(&self) -> f64 {
        self.inner.signal_level()
    }
    #[getter]
    fn samples_decoded(&self) -> u64 {
        self.inner.samples_decoded()
    }
}

#[pyclass(module = "timecode_tools._native")]
struct NativeLtcReceiver {
    inner: LtcReceiver,
}

#[pymethods]
impl NativeLtcReceiver {
    #[new]
    fn new(
        frame_rate: Option<&str>,
        offset_frames: i64,
        lost_after: f64,
        channel: u32,
    ) -> PyResult<Self> {
        Ok(Self {
            inner: LtcReceiver::new(receiver_options(
                frame_rate,
                offset_frames,
                lost_after,
                Some(channel),
            )?),
        })
    }

    fn push(
        &mut self,
        data: &[u8],
        float: bool,
        sample_rate: u32,
        channels: u16,
        timestamp: f64,
    ) -> PyResult<Vec<NativeEvent>> {
        let pcm = Pcm::new(data, float)?;
        let chunk = PcmChunk {
            samples: pcm.samples(),
            sample_rate,
            channels,
            timestamp_ns: Some(s_to_ns(timestamp)?),
        };
        Ok(events(self.inner.push(chunk).map_err(ltc_error)?))
    }

    fn signal_lost(&mut self) -> Vec<NativeEvent> {
        events(self.inner.signal_lost())
    }

    #[getter]
    fn locked(&self) -> bool {
        self.inner.locked()
    }
    #[getter]
    fn detected_rate(&self) -> Option<&'static str> {
        self.inner.detected_rate().map(|r| r.name())
    }
    #[getter]
    fn level(&self) -> f64 {
        self.inner.level()
    }
    #[getter]
    fn offset_frames(&self) -> i64 {
        self.inner.offset_frames()
    }
    #[setter]
    fn set_offset_frames(&mut self, frames: i64) {
        self.inner.set_offset_frames(frames);
    }
    /// Fractional frame count since 00:00:00:00 at `timestamp` (seconds, the
    /// chunk timestamp clock), offset applied; None until fitted.
    fn frames_at(&self, timestamp: f64) -> PyResult<Option<f64>> {
        Ok(self.inner.frames_at(s_to_ns(timestamp)?))
    }
}

#[pyclass(module = "timecode_tools._native")]
struct NativeMtcReceiver {
    inner: MtcReceiver,
}

#[pymethods]
impl NativeMtcReceiver {
    #[new]
    fn new(frame_rate: Option<&str>, offset_frames: i64, lost_after: f64) -> PyResult<Self> {
        Ok(Self {
            inner: MtcReceiver::new(receiver_options(
                frame_rate,
                offset_frames,
                lost_after,
                None,
            )?),
        })
    }

    fn push(&mut self, data: &[u8], timestamp: f64) -> PyResult<Vec<NativeEvent>> {
        let chunk = MidiChunk {
            data,
            timestamp_ns: s_to_ns(timestamp)?,
        };
        match self.inner.push(chunk) {
            Ok(list) => Ok(events(list)),
            Err(never) => match never {},
        }
    }

    fn signal_lost(&mut self) -> Vec<NativeEvent> {
        events(self.inner.signal_lost())
    }

    #[getter]
    fn locked(&self) -> bool {
        self.inner.locked()
    }
    #[getter]
    fn detected_rate(&self) -> Option<&'static str> {
        self.inner.detected_rate().map(|r| r.name())
    }
    #[getter]
    fn offset_frames(&self) -> i64 {
        self.inner.offset_frames()
    }
    #[setter]
    fn set_offset_frames(&mut self, frames: i64) {
        self.inner.set_offset_frames(frames);
    }
    /// Fractional frame count since 00:00:00:00 at `timestamp` (seconds, the
    /// chunk timestamp clock), offset applied; None until fitted.
    fn frames_at(&self, timestamp: f64) -> PyResult<Option<f64>> {
        Ok(self.inner.frames_at(s_to_ns(timestamp)?))
    }
}

/// (hours, minutes, seconds, frames, drop_frame)
type NativeLabel = (u8, u8, u8, u8, bool);

fn check_fps(fps: u32) -> PyResult<()> {
    if [24, 25, 30].contains(&fps) {
        Ok(())
    } else {
        Err(invalid(format!("fps must be 24, 25 or 30, got {fps}")))
    }
}

fn to_label((hours, minutes, seconds, frames, drop_frame): NativeLabel) -> Label {
    Label {
        hours,
        minutes,
        seconds,
        frames,
        drop_frame,
    }
}

/// Frame count since 00:00:00:00 for a label (no validation of the label).
#[pyfunction]
fn frame_index(label: NativeLabel, fps: u32) -> PyResult<i64> {
    check_fps(fps)?;
    Ok(to_label(label).frame_index(fps))
}

/// Label for a frame count, wrapping around one day.
#[pyfunction]
fn label_from_index(index: i64, fps: u32, drop_frame: bool) -> PyResult<NativeLabel> {
    check_fps(fps)?;
    let l = Label::from_index(index, fps, drop_frame);
    Ok((l.hours, l.minutes, l.seconds, l.frames, l.drop_frame))
}

#[pyfunction]
fn frames_per_day(fps: u32, drop_frame: bool) -> PyResult<i64> {
    check_fps(fps)?;
    Ok(day_length(fps, drop_frame))
}

// Test-signal helpers (used by the package's tests; not public API).

/// Mono 16-bit LTC test signal, as native-endian bytes.
#[pyfunction]
fn generate_ltc(
    sample_rate: u32,
    frame_rate: &str,
    start_index: i64,
    frame_count: u32,
    amplitude: f64,
) -> PyResult<Vec<u8>> {
    let rate = fixed_rate(frame_rate)?;
    if !(0. ..=1.).contains(&amplitude) || frame_count > 24 * 3600 * 30 {
        return Err(invalid("amplitude or frame_count out of range"));
    }
    let signal = generate(SynthConfig {
        sample_rate,
        rate: rate.fps(),
        nominal_fps: rate.nominal_fps(),
        drop_frame: rate.drop_frame(),
        start_index,
        frame_count: frame_count as usize,
        amplitude: amplitude * 32767.,
        ..Default::default()
    });
    Ok(signal.pcm.iter().flat_map(|s| s.to_ne_bytes()).collect())
}

/// MTC test data: the quarter-frame messages (two bytes each, concatenated)
/// and the send time of each in seconds, from 0.
#[pyfunction]
fn generate_mtc(
    frame_rate: &str,
    start_index: i64,
    frame_count: u32,
) -> PyResult<(Vec<u8>, Vec<f64>)> {
    let rate = fixed_rate(frame_rate)?;
    if frame_count > 24 * 3600 * 30 {
        return Err(invalid("frame_count must be at most one day"));
    }
    let messages = mtc::quarter_frames(rate, start_index, frame_count as usize);
    let bytes = messages.iter().flat_map(|(m, _)| *m).collect();
    let times = messages.iter().map(|(_, t)| *t as f64 / 1e9).collect();
    Ok((bytes, times))
}

/// Full-frame SysEx message for a label.
#[pyfunction]
fn mtc_full_frame(frame_rate: &str, label: NativeLabel) -> PyResult<Vec<u8>> {
    let rate = fixed_rate(frame_rate)?;
    let label = Label {
        drop_frame: rate.drop_frame(),
        ..to_label(label)
    };
    Ok(mtc::full_frame(rate, label).to_vec())
}

#[pymodule]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<NativeLtcDecoder>()?;
    m.add_class::<NativeLtcReceiver>()?;
    m.add_class::<NativeMtcReceiver>()?;
    m.add_function(wrap_pyfunction!(frame_index, m)?)?;
    m.add_function(wrap_pyfunction!(label_from_index, m)?)?;
    m.add_function(wrap_pyfunction!(frames_per_day, m)?)?;
    m.add_function(wrap_pyfunction!(generate_ltc, m)?)?;
    m.add_function(wrap_pyfunction!(generate_mtc, m)?)?;
    m.add_function(wrap_pyfunction!(mtc_full_frame, m)?)?;
    Ok(())
}
