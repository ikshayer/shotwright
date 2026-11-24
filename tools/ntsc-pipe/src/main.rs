use std::env;
use std::fs;
use std::io::{self, Read, Write};
use std::process::ExitCode;

use ntsc_rs::settings::{SettingsList, standard::UseField};
use ntsc_rs::yiq_fielding::Rgb;
use ntsc_rs::{Context, NtscEffect};

#[derive(Debug)]
struct Args {
    width: usize,
    height: usize,
    frames: Option<u64>,
    seed: i32,
    frame_index: usize,
    settings: String,
}

enum CliError {
    Failure(String),
    RawFrameProtocol(String),
}

impl From<String> for CliError {
    fn from(value: String) -> Self {
        Self::Failure(value)
    }
}

impl CliError {
    fn exit_code(&self) -> u8 {
        match self {
            Self::Failure(_) => 1,
            Self::RawFrameProtocol(_) => 2,
        }
    }
}

fn parse_args() -> Result<Args, String> {
    let mut width = None;
    let mut height = None;
    let mut frames = None;
    let mut seed = None;
    let mut frame_index = 0usize;
    let mut settings = None;

    let mut values = env::args().skip(1);
    while let Some(key) = values.next() {
        let mut value = || -> Result<String, String> {
            values
                .next()
                .ok_or_else(|| format!("missing value for {key}"))
        };
        match key.as_str() {
            "--width" => width = Some(parse_positive(&key, &value()?)?),
            "--height" => height = Some(parse_positive(&key, &value()?)?),
            "--frames" => {
                let raw = value()?;
                let count = raw
                    .parse::<u64>()
                    .map_err(|_| format!("invalid {key}: {raw}"))?;
                if count == 0 {
                    return Err("--frames must be positive".into());
                }
                frames = Some(count);
            }
            "--seed" => {
                let raw = value()?;
                seed = Some(
                    raw.parse::<i32>()
                        .map_err(|_| format!("invalid {key}: {raw}"))?,
                );
            }
            "--frame-index" => {
                let raw = value()?;
                frame_index = raw
                    .parse::<usize>()
                    .map_err(|_| format!("invalid {key}: {raw}"))?;
            }
            "--settings" => settings = Some(value()?),
            "--help" | "-h" => return Err(usage().into()),
            _ => return Err(format!("unknown argument: {key}\n{}", usage())),
        }
    }

    Ok(Args {
        width: width.ok_or_else(|| format!("--width is required\n{}", usage()))?,
        height: height.ok_or_else(|| format!("--height is required\n{}", usage()))?,
        frames,
        seed: seed.ok_or_else(|| format!("--seed is required\n{}", usage()))?,
        frame_index,
        settings: settings.ok_or_else(|| format!("--settings is required\n{}", usage()))?,
    })
}

fn parse_positive(name: &str, raw: &str) -> Result<usize, String> {
    let value = raw
        .parse::<usize>()
        .map_err(|_| format!("invalid {name}: {raw}"))?;
    if value == 0 {
        return Err(format!("{name} must be positive"));
    }
    Ok(value)
}

fn checked_frame_bytes(width: usize, height: usize) -> Result<usize, String> {
    if width == 0 || height == 0 {
        return Err("frame width and height must be positive".into());
    }
    width
        .checked_mul(height)
        .and_then(|pixels| pixels.checked_mul(3))
        .ok_or_else(|| "width x height x 3 overflows this platform".to_string())
}

fn checked_frame_index(start: usize, processed: u64) -> Result<usize, String> {
    start
        .checked_add(usize::try_from(processed).map_err(|_| "frame count exceeds usize")?)
        .ok_or_else(|| "frame index overflows usize".into())
}

fn validate_effect_geometry(
    width: usize,
    height: usize,
    effect: &NtscEffect,
) -> Result<(), String> {
    if width < 4 {
        return Err("ntsc-rs requires a frame width of at least 4 pixels".into());
    }
    let field_rows = match effect.use_field {
        UseField::Both => height,
        UseField::Upper => height.div_ceil(2),
        UseField::Lower
        | UseField::Alternating
        | UseField::InterleavedUpper
        | UseField::InterleavedLower => height / 2,
    };
    if field_rows < 2 {
        return Err(format!(
            "ntsc-rs requires at least 2 rows in each processed field; {width}x{height} is too small for {:?}",
            effect.use_field,
        ));
    }
    Ok(())
}

fn checked_effect_frame_index(frame_num: usize, field_mode: UseField) -> Result<(), String> {
    if matches!(
        field_mode,
        UseField::InterleavedUpper | UseField::InterleavedLower
    ) && frame_num > (usize::MAX - 1) / 2
    {
        return Err("interleaved field frame index overflows usize".into());
    }
    Ok(())
}

fn parse_settings(settings_json: &str) -> Result<ntsc_rs::NtscEffect, String> {
    SettingsList::<NtscEffect>::new()
        .from_json(settings_json)
        .map_err(|error| format!("invalid ntsc-rs settings JSON: {error}"))
}

fn usage() -> &'static str {
    "usage: ntsc-pipe --width W --height H [--frames N] --seed I32 --settings FILE [--frame-index N]\n\
     Reads raw rgb24 frames from stdin and writes same-size rgb24 frames to stdout.\n\
     Omit --frames to process complete frames until EOF; --frame-index defaults to 0."
}

fn read_frame(input: &mut impl Read, frame: &mut [u8]) -> io::Result<bool> {
    let mut first = [0u8; 1];
    match input.read(&mut first)? {
        0 => return Ok(false),
        1 => frame[0] = first[0],
        _ => unreachable!(),
    }
    input.read_exact(&mut frame[1..])?;
    Ok(true)
}

fn run() -> Result<(), CliError> {
    let args = parse_args().map_err(CliError::Failure)?;
    let frame_bytes = checked_frame_bytes(args.width, args.height).map_err(CliError::Failure)?;

    let settings_json = fs::read_to_string(&args.settings).map_err(|error| {
        CliError::Failure(format!(
            "could not read settings file {}: {error}",
            args.settings
        ))
    })?;
    let mut effect = parse_settings(&settings_json).map_err(CliError::Failure)?;
    validate_effect_geometry(args.width, args.height, &effect).map_err(CliError::Failure)?;
    effect.random_seed = args.seed;

    let ctx = Context::new();
    let mut input = io::stdin().lock();
    let mut output = io::stdout().lock();
    let mut frame = Vec::new();
    frame.try_reserve_exact(frame_bytes).map_err(|error| {
        format!("could not allocate {frame_bytes} bytes for one rgb24 frame: {error}")
    })?;
    frame.resize(frame_bytes, 0);
    let mut count = 0u64;

    loop {
        if args.frames.is_some_and(|limit| count == limit) {
            let mut extra = [0u8; 1];
            if input
                .read(&mut extra)
                .map_err(|error| CliError::Failure(format!("stdin read failed: {error}")))?
                != 0
            {
                return Err(CliError::RawFrameProtocol(
                    "stdin contains more bytes than --frames permits".into(),
                ));
            }
            break;
        }

        let has_frame = read_frame(&mut input, &mut frame).map_err(|error| {
            CliError::RawFrameProtocol(format!(
                "stdin does not contain a complete rgb24 frame: {error}"
            ))
        })?;
        if !has_frame {
            if let Some(limit) = args.frames {
                return Err(CliError::RawFrameProtocol(format!(
                    "stdin ended after {count} frames; --frames requested {limit}"
                )));
            }
            break;
        }

        let frame_num = checked_frame_index(args.frame_index, count).map_err(CliError::Failure)?;
        checked_effect_frame_index(frame_num, effect.use_field).map_err(CliError::Failure)?;
        effect.apply_effect_to_buffer::<Rgb, u8>(
            &ctx,
            (args.width, args.height),
            &mut frame,
            frame_num,
            [1.0, 1.0],
        );
        output
            .write_all(&frame)
            .map_err(|error| CliError::Failure(format!("stdout write failed: {error}")))?;
        count += 1;
    }

    output
        .flush()
        .map_err(|error| CliError::Failure(format!("stdout flush failed: {error}")))?;
    Ok(())
}

fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            match &error {
                CliError::Failure(message) | CliError::RawFrameProtocol(message) => {
                    eprintln!("ntsc-pipe: {message}");
                }
            }
            ExitCode::from(error.exit_code())
        }
    }
}

#[cfg(test)]
mod tests {
    use super::{
        CliError, checked_effect_frame_index, checked_frame_bytes, checked_frame_index,
        parse_settings, read_frame, validate_effect_geometry,
    };
    use ntsc_rs::settings::standard::UseField;
    use std::io::Cursor;

    #[test]
    fn frame_byte_count_is_packed_rgb24_and_overflow_checked() {
        assert_eq!(checked_frame_bytes(640, 480).unwrap(), 640 * 480 * 3);
        assert!(
            checked_frame_bytes(0, 480)
                .unwrap_err()
                .contains("positive")
        );
        assert!(
            checked_frame_bytes(usize::MAX, 2)
                .unwrap_err()
                .contains("overflows")
        );
    }

    #[test]
    fn frame_reader_distinguishes_eof_complete_and_partial_frames() {
        let mut empty = Cursor::new(Vec::<u8>::new());
        let mut frame = [0_u8; 6];
        assert!(!read_frame(&mut empty, &mut frame).unwrap());

        let mut full = Cursor::new(vec![1, 2, 3, 4, 5, 6]);
        assert!(read_frame(&mut full, &mut frame).unwrap());
        assert_eq!(frame, [1, 2, 3, 4, 5, 6]);

        let mut partial = Cursor::new(vec![1, 2, 3]);
        assert!(
            read_frame(&mut partial, &mut frame)
                .unwrap_err()
                .to_string()
                .contains("failed to fill")
        );
    }

    #[test]
    fn frame_index_preserves_the_starting_index_and_checks_overflow() {
        assert_eq!(checked_frame_index(4, 0).unwrap(), 4);
        assert_eq!(checked_frame_index(4, 3).unwrap(), 7);
        assert!(
            checked_frame_index(usize::MAX, 1)
                .unwrap_err()
                .contains("overflows")
        );
    }

    #[test]
    fn interleaved_frame_index_rejects_the_librarys_doubled_index_overflow() {
        assert!(checked_effect_frame_index(usize::MAX / 2, UseField::InterleavedUpper).is_ok());
        assert!(
            checked_effect_frame_index(usize::MAX / 2 + 1, UseField::InterleavedUpper,)
                .unwrap_err()
                .contains("interleaved field frame index")
        );
        assert!(
            checked_effect_frame_index(usize::MAX / 2 + 1, UseField::InterleavedLower,).is_err()
        );
    }

    #[test]
    fn effect_geometry_rejects_dimensions_too_small_for_each_processed_field() {
        let effect = parse_settings(r#"{"version":1,"use_field":4}"#).unwrap();
        assert!(
            validate_effect_geometry(4, 2, &effect)
                .unwrap_err()
                .contains("at least 2 rows in each processed field")
        );
        assert!(validate_effect_geometry(4, 4, &effect).is_ok());
        assert!(
            validate_effect_geometry(3, 4, &effect)
                .unwrap_err()
                .contains("width of at least 4")
        );
    }

    #[test]
    fn settings_json_uses_the_pinned_settings_list_and_reports_parse_errors() {
        let effect =
            parse_settings(r#"{"version":1,"head_switching":true,"head_switching_height":7}"#)
                .unwrap();
        assert!(effect.head_switching.enabled);
        assert_eq!(effect.head_switching.settings.height, 7);
        assert!(
            parse_settings(r#"{"head_switching":true}"#)
                .unwrap_err()
                .contains("invalid ntsc-rs settings JSON")
        );
    }

    #[test]
    fn raw_frame_protocol_failures_have_a_distinct_exit_code() {
        assert_eq!(
            CliError::RawFrameProtocol("short stream".into()).exit_code(),
            2
        );
        assert_eq!(CliError::Failure("bad settings".into()).exit_code(), 1);
    }
}
