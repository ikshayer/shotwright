# Contained read design

`ProjectRoot.read_bytes(relative)` and `read_text(relative, *, encoding="utf-8")` each
call the existing `resolve(relative)` containment check once. The default opener is
`Path.open("rb")`; it retries `PermissionError` on the existing bounded schedule and
raises `ContainedReadError`, a `PermissionError` subclass, when that schedule is
exhausted. Other errors, including missing files, pass through. `read_text` wraps the
binary stream with strict decoding and universal-newline translation, matching
`Path.read_text()`; CRLF and bare CR become LF. `read_bytes` preserves every byte.

`Settings.use_windows_share_delete_reads` defaults to `false`. When enabled, each
`ProjectRoot` created from those settings uses the Windows `CreateFileW` path with
`FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE`, adopted through
`msvcrt.open_osfhandle`. The flag has no effect on non-Windows platforms.

## Why the expensive path is optional

The original rationale was that `FILE_SHARE_DELETE` would let `os.replace` replace a
file while a read handle was open. Amendment 2 of `ATOMIC_WRITE_CONTRACT.md` measured
that premise on this machine and disproved it: a second handle requesting `DELETE`
access opened successfully, but `os.replace`, `MoveFileExW`, and `ReplaceFileW` all
failed with `WinError 5` until the reader closed. The permissive share mode therefore
does not fix the observed replacement failure. The path is retained as an option, not a
recommendation, and it is not known to fix anything. It has measurable per-read cost.

The default open path addresses only the residual transient refusal: a read can overlap
the short replace window on a file written through the atomic writer. It retries a
bounded number of times, then fails loudly; it never falls back to the other opener.
This retry cannot make a writer's replacement succeed while a handle remains open.

## Migration notes

The contained read surface is used for project-local reads in the store, both GPU
adapters, queue, and lease. `settings.py` continues to read its caller-selected
`settings.toml` directly because that file may be outside a project root. Blender's
packaged `worker.py` is also read directly because it is package source outside the
project root. The current API reads job reports through the append-only job journal;
that journal is deliberately unchanged.

The lease JSON record is read through `ProjectRoot` while its separate OS lock is held.
The lock file itself is not replaced or read through this API.
