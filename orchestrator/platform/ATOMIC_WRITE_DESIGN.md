# Atomic `ProjectRoot` write design

`write_bytes` and `write_text` stage each complete payload in the reserved
project-root directory `.projectroot-atomic-write/`, using the name
`write-<32 lowercase hex digits>.tmp`. The staging directory is created through
`ProjectRoot.make_dir`; each generated relative file path is resolved through
`ProjectRoot` before opening it. A contained symlink or junction at the reserved
staging path is rejected. Before writing, the code compares the staging and target
parent filesystem device ids; cross-filesystem targets fail before replacement.
For accepted writes, `os.replace` therefore stays on one filesystem.

The project-size walk prunes the reserved directory at the project root. Project
listing still inspects that directory entry and attempts to load its project record;
an ordinary staging directory has no project record and is ignored, while a valid
project using that name remains visible. The remaining `iterdir`/`glob` calls
enumerate record-specific domain folders, cascade journals, or `state/jobs`, none of
which contains the root-level staging directory. The audited sites are recorded in
the implementation report.

`os.replace` is attempted at most five times. A `PermissionError` waits 10, 20,
40, then 80 milliseconds before the next attempt, for a maximum planned wait of
150 milliseconds. Exhaustion raises `AtomicWriteError`; there is no in-place
fallback. A failed staging or replace operation retries temp-file cleanup with the
same bounds and raises `AtomicWriteError` if cleanup still fails. A persistent OS
handle that refuses deletion can prevent cleanup despite these retries; that case is
reported rather than hidden.

Writes are atomic, not durable: there is no `fsync` of the file or directory.

## Contract limits found during review

The staging directory name is reserved by convention but `ProjectRoot` still accepts
it as a caller destination. A caller writing that exact name conflicts with directory
creation; caller data below that root-level name is excluded from project-size
accounting. Resolving that collision would require either reserving/rejecting caller
paths or changing the staging/enumeration scheme, which changes the allowed-path
contract.

Requirement 7's absolute cleanup guarantee cannot be met if the OS persistently
denies deletion after all bounded retries. `AtomicWriteError` reports that failure;
the temp file may remain. Also, `ProjectStore` has a separate sibling-temp writer
through `write_with`; requirement 4 needs an explicit scope decision if it includes
those pre-existing artifacts.
