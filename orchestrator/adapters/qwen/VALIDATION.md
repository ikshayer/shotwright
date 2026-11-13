# Qwen helper adapter validation

Completed 2026-11-12UTC in `D:/Work/video_pipeline/.claude/worktrees/api-conformance`. User-selected interface: LOCAL CLI; overlapping windows; generated-audio review REQUIRES an intent brief; default 25 seconds/2 seconds minimum overlap/500 output tokens. The helper is advisory and separate from production API/job/approval mutation.

## Implemented interface

`python -B -m orchestrator.adapters.qwen analyze|review|inspect`, plus Python API `QwenAdapter`/`AnalysisRequest`. Inputs are contained project-relative files; real model backend is restricted to this repository and reuses the existing isolated Qwen runtime. No model/environment installation was performed. Review accepts `--brief` or `--brief-file`; no brief is an error. Dryrun plans without model inference. Complete JSON/Markdown reports retain exact rawresponses, independentbrief, physicalsignal metrics, perwindow/sourceintervals, overlappingcoverage, nativeprovenance and EOS/cap flags. Failures preserve validcompletedprefix and uncoveredintervals. No automaticretry/fallback or automaticapplication of recommendations.

The only shared analyzer edit is `except Exception` -> `except BaseException` in the owned-worker cleanup guard. Loader,model,quantization,decoder,seed,generation defaults and RAM/GPU/watchdog thresholds are unchanged. Existing process-tree supervision is reused rather than replaced.

## Verification actually run

| Check | Result | Evidence |
|---|---|---|
| Frozen independent adapter suite | 89 passed, 25.24 s; source unchanged | `_work/qwen_helper_validation/9a1e93701c7947f29cc1ce4a4048f1ab/` |
| Full repository pytest | 1833 passed, 50 skipped, 6 warnings, 458.34 s; source unchanged | `_work/qwen_helper_fullsuite/f691867035d7411ea727e6d810683640/` |
| Existing analyzer CPU/serialization/generation/supervisor checks | 121 passed | `spikes/23_joint_av_analyzer/_work/verification/20261112T050839Z_d5bf6319/report.json` |
| Whole orchestrator/tests Ruff |Passed, no cache | Parentexecution |
| RealCLI inspect |Passed without CUDA/modelload | Parentexecution |
| Reference+review dryplans |Both passed | Integration summarybelow |
| Actual reference 35 seconds |2 overlapping 25 s answers,413/383tokensEOS | `_work/qwen_helper/20261112T052847Z_105e0cbe/report.json` |
| Actual generated 8 s review withbrief |320 tokens EOS | `_work/qwen_helper/20261112T053410Z_1e1832ad/report.json` |
| Parent actualPCM/frame/raw/provenance/freshPID audit |Passed, all 3 prepared windows | `_work/qwen_helper_integration/24a6dc71f7284e7d8e18085632c036f0/parent_actual_audit.json` |
| Independent analytic nativeaudio metrics |RMS/peak/DC/quietfraction/anticorrelatedstereo/samplecount matched closed-form finitewindow signal | `_work/qwen_helper_validation/metrics_7eda14729fe043caa9b71deda47df120/independent_analytic_validation.json` |

The 89 tests include actualWindowslauncher/descendant KeyboardInterrupt andSystemExit cleanup withunrelatedsentry survival, stable nonblocking multiprocesslock identity, nativeFFmpegPCM equality/stereo/sample-rate/framecount/silence/nonfinite/shiftedPTS checks, and fakebackend batching/EOS/cap/partialfailure/unknowncleanup/invalidpayload/UnicodeCLI cases. Model inference is not faked in the separate actualCLI smoke runs. Read-only contract/source and terminalartifact reviews were performed by a separate reviewer. Prior failingprototype reports are retained; they are not substituted for these frozenfinalpasses.

## Actual reports and measured limits

The reference request spans source [1397,1432),23:17?23:52. Plannedinput intervals[0,25] and[10,35] cover it with15seconds final overlap (the configured2seconds is the minimum). Eachpreparedwindow has600video frames,1200000native48kHzstereosamples/channel and9,600,000PCMbytes. Every decodedpreparedframe and PCMbyte matches the corresponding source slice. Native model input remains20sampledframes/.8fps and continuous16kHzmono per25s.

The generatedcandidate is the retained8s directH3 file `spikes/22_minimax_h3_workflows/_work/direct_comfy/20261111T031416Z_d4ce1b8d/output/direct_easy_00001_.mp4`. The actualreview preserved192video frames and256000native32kHzstereosamples/channel (2,048,000PCMbytes) byte-for-byte. Requiredbrief described continuousaudiblehiss andquietfloorboardcreaks, no prominentbootimpacts/drilling, and asked to assess ratherthanassume theywerepresent.

| Actualrun | Native supervisedtime | WholeGPUbaseline/peak/post | MinimumsampledRAM | PeakownedRSS |
|---|---:|---:|---:|---:|
| Reference2-windowbatch |308.468s |1501/11720/1521MiB |3.801GiB |2.643GiB |
| Generatedreview |131.437s |1521/10653/1585MiB |4.737GiB |2.300GiB |

Both runs had empty native loading diagnostics,196NF4language/420unquantizedcoverage, exactexpectedgeneration configuration, matching pinnedshard/runtimeprovenance and verifiedcleanup. Allsixcapturedidentities were absent atparentfreshchecks; no analyzerjobs remained. Integration source identities matchedbefore/after/current. Four selectedexisting-environment fingerprints matched their retainedbeforevalues.

Five-second whole-device samples includeforeignusage and canmisspeaks. These areobservations, not minimumrequirements or universalcapacity. Admission remains6GiBavailableRAM/8GiBfreeGPU/8GiBdisk; runtime stop is<2GiBavailableRAM for three samples or 900 seconds. Exactpost-import admissionRAM value was not retained.25 seconds had 225 MiB minimumsampledfreeGPU in thisintegration, so headroom islimited.

## Perceptual QA remains advisory

The8s candidate measured channelRMS -63.83/-62.78dBFS; 98.7615% of samples werebelow -60 dBFS. Qwen nevertheless asserted continuoushiss andquietcreaks werewellmatched to thebrief. This demonstrates successfultoolplumbing, not a validatedqualitypass. Neither that candidate nor any productionversion was approved. Referencecaptions continue to include unsupportedconversational and other interpretations. Agents mustread physicalmetrics, creatorfeedback and rawuncertainties, not silentlyturn modelopinions into facts.

## Scope and residual limitations

- CLI-only, machine-local advisoryadapter. It doesnotreplace FR18 productionreferences.sample/stylebible interpretation or register Qwen in the productionGPUlease/queue. Futureproductionintegration requires PRD8.2/8.3 modelslot/budget work.
- The helperstablelock serializes helpercalls only. A liveproductionlease ischecked, but production canstartafterthatprecheck; foreignGPUapps remainuncoordinated. No appwas stopped.
- The native supervisor stillinherits anunbounded process.wait inexceptioncleanup; actually testedinterruption cleanup passed, not a universalboundedcancellation claim. CPUtool handles arekilled/reaped ontimeout/interrupt; arbitrary subprocessdescendant behavior ofotherbinaries isnotcertified.
- Localinputprotocols arefile/pipe and entry/outputpaths arecontained. Use self-containedlocalmedia. A separate demuxer-formatallowlist and arbitrarynestedlocalcontainer-reference validation areNOTimplemented; thisfirstversion isnot anuntrusteduploadendpoint. The initialcontract's broaderformatallowlist requirement is deferred explicitly; do notclaim network-free implies every indirect localread iscontained.
- Qwen sees sparseframes and16kHzmono analysis audio. Native sourcePCM isretained separately; rapidcamera shake, finevoices/transcripts/identities and physicalsoundcauses are not certified.
- READMEinitialdirectwrite bypassedProjectRoot once, ontheassignedfileonly; itwas rewritten throughthehelper. Processnote retained at `spikes/23_joint_av_analyzer/_work/adapter_build_20261112/adjudication.json`. Nooutsidewrite wasreported. Finalwrites use containment.

No commits,pushes,merges,publication,modeldownloads,environmentinstalls,deletions,sourceoverwrites,productionentity/approval changes or additionalinference arepending.
