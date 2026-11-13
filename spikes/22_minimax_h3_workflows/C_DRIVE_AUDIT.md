# C drive capacity audit - no deletion performed

2026-11-09, during local H3 preparation. Creator requested C cleanup and expressly requested notice before deleting anything. Only read-only inspection has occurred. Proposed actions await approval.

Observed C capacity360660144128bytes, free0bytes. D free83313770496bytes at first snapshot (~77.59GiB). Current H3 runtime/wheels/model targets/cache are on D. A read-only downloader check found its write bytes match the D partial and process RSS~49MB/private~38MB; no open C model/cache target was found. It inherited C Temp but the raw urllib writer explicitly uses D. Codex daemon stderr logs checked were only~14KB/~10KB, not a material capacity contributor.

## Material files and recent data

- C:/pagefile.sys42604292096bytes (39.678GiB), modified21:34:29UTC. Registry PagingFiles ?: pagefile.sys / ExistingPageFiles C confirms automatic configuration. Windows system-managed pagefiles can expand with commit pressure. Current process-reported systemcommit55.695GiB of58.292GiB, available2.597GiB; RAMavailable3.747GiB at first audit. No before-size baseline exists, so exact growth or attribution to our setup is not proven. Do not delete/shrink this active memory backing as routine cache cleanup.
- C:/hiberfil.sys13365088256bytes (12.447GiB), modified20:20:45UTC. This supports power features; disabling hibernation changes behavior and requires a separate explicit decision, not routine deletion.
- C:/Users/spark/AppData/Local/Temp/wireshark_Wi-Fi0O4NY3.pcapng5592688413bytes (5.209GiB). Createdtoday13:17local, modified17:04local; size unchanged across audit snapshots. Packet capture may include model-download traffic even though downloads themselves are on D, but contents were not read and exact traffic attribution is unproven. Wireshark process is present; open-file query denied. Preserve this user diagnostic until approved.
- C:/Users/spark/AppData/Local/Temp/DiagOutputDir/RdClientAutoTrace8413257728bytes (7.836GiB),880ETLfiles modifiedsinceNov6/latesttoday17:08. Grew~3MB between metadata scans. Likely remote-desktop automatic diagnostics based on path; no blanket cleanup approved.

## Concrete initial cleanup proposal

Clear only C:/Users/spark/AppData/Local/pip/cache (5647605899bytes/5.260GiB) and C:/Users/spark/AppData/Local/npm-cache/_cacache (9815551955bytes/9.141GiB), total15463157854bytes/~14.401GiB logical. Both resolved to exactly the expected user-cache paths and root entries were not reparse points. These are regenerative package-download caches; future installs may re-download. Preserve installed environments and npm-cache/_npx. Exact reclaimed bytes may differ due to active locks/allocation and will be measured after approved cleanup. No file deletion or move has occurred.

Other findings withheld from initial proposal: UVcache14.022GiB may support cached tool environments; NVIDIA DXCache8.090GiB can be active/rebuild; CrashDumps0.938GiB contain diagnostics; WSLext4.vhdx14.755GiB is a Linux filesystem, not disposable cache; HFChatterboxcache3.110GiB/rembgmodel0.954GiB are model assets. Browsers' small caches are lower priority and profiles/sessions/credentials must be preserved. Temp scan encountered3556errors and14.04GiB is a lower bound.

## Ongoing preparation and approval review

H3 generation remains held for RAM; creator chose Continue preparation / free memory later. Native/Easy runtime installation and parent independent verification completed; no H3 inference. Main raw model download on D remains running. Auto-review rejected the worker's exact-process stop + aborted audit action as outside the new C-cleanup scope; it did not execute and no workaround was attempted. To stop it now requires explicit user approval; preserve existing model/partial files. Already-running code contains pre-existing failure cleanup, so do not claim it was paused or guarantee future automatic behavior without resolving the owned process.

Sources: [Microsoft pagefile behavior](https://learn.microsoft.com/en-us/troubleshoot/windows-client/performance/how-to-determine-the-appropriate-page-file-size-for-64-bit-versions-of-windows), [pip caching](https://pip.pypa.io/en/stable/topics/caching/), [npm cache](https://docs.npmjs.com/cli/cache/). Metadata timestamps establish recency, not a historical free-space baseline.


## Approved cleanup completed

Creator explicitly approved the package-download cache cleanup. Native PowerShell removed only pip/cache and npm-cache/_cacache contents, with exact root/child checks, no reparse traversal, file-by-file deletion and empty-directory removal. Zero errors; both root directories retained. Deleted19346files/9837399282logicalbytes (~9.16GiB). Pip was already reduced externally to21847327bytes before our action; do not claim its earlier5.26GiB removal. npm deleted9815551955bytes. Parent independently rescanned both targets:0files/0bytes/0subdirectories, no errors; sibling_npx still exists.

Cfree immediately afterward was29266755584bytes; parent recheck29265956864bytes (~27.256GiB). The free-space increase exceeded the bytes we removed, so other external/background changes also freed capacity. No claim that all27GiB was recovered by our cleanup. Execution result and parent verification are in _work/c_drive_audit/. No other cache/model/personal/system file or process was touched. The model-download stop remains unapproved and was not performed.
