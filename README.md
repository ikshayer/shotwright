# shotwright

Local, agent-operated production pipeline for CCTV-style analog horror: it plans episodes and
shots, builds and renders scenes, generates audiovisual candidates, degrades them to look like
tape, and keeps a provenance receipt for everything it made.

```
FLUX  ──►  TRELLIS + Blender  ──►  MiniMax H3  ──►  post profile  ──►  cut
stills      3D assets, scenes      video + audio     tape degrade      episode
                                        │
                                        ▼
                              Qwen2.5-Omni (local)
                           joint audio + video review
```

Everything runs on one machine. The orchestrator binds to loopback only and requires a token, so
nothing is exposed to the network.

## Why it is built this way

- **Agents operate it, people judge it.** Every capability is an HTTP operation with a typed
  request model, and the agent tool manifest is derived from the live FastAPI route table rather
  than hand-maintained, so the tools cannot drift from the API.
- **Provenance is not optional.** Generated outputs carry receipts recording the model, inputs and
  the hash of the script that produced them. Asset credits are collected per episode, and the
  credits endpoint reports **publication blockers** — assets whose licence or reuse scope does not
  clear redistribution.
- **Long work is queued, not blocked.** Scene builds, renders, generation and degradation are
  submitted to a job queue and answered with a job record; the rest of the API stays responsive.
- **Review is advisory, never automatic.** A local Qwen2.5-Omni helper watches a take and writes
  notes on it — `joint` audio-and-video together, or `audio` alone — judged against a creator's
  stated intent rather than against clean studio fidelity, so deliberate hiss and distortion read
  as intended rather than as faults. Its output is text for a person to weigh: it cannot approve a
  shot or change any project entity, and it runs outside the API, the job queue and the production
  GPU lease so a review can never contend with a render.
- **Determinism where it matters.** Post profiles and timeline assembly are reproducible so a shot
  can be re-derived rather than remembered.

## Status

Phase 1 is complete and scored against a rubric (`PHASE1_RUBRIC_SCORE.md`,
`PHASE1_TRACEABILITY.md`). The `spikes/` directory holds 23 dated investigations, each with a
contract and findings — they are the research record behind the design, not supported surface.

## Install

Requires Python 3.11+. An NVIDIA GPU is needed for the generation stages; the orchestrator and its
tests run without one.

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"   # Linux/macOS: .venv/bin/pip
```

## Use

The CLI exposes one subcommand per API operation, in both `snake_case` and `hyphenated` form:

```bash
cctv-pipeline --project-root <dir> health
cctv-pipeline --project-root <dir> create-project --help
cctv-pipeline --settings settings.toml list-projects
```

Or serve the API and drive it over HTTP or from an MCP client. Make a token once and
keep it in the environment, never on a command line. In PowerShell this stores one in
your user environment without displaying it (open a new terminal afterwards):

```powershell
$token = python -c "import secrets; print(secrets.token_hex(32))"
[Environment]::SetEnvironmentVariable("CCTV_PIPELINE_TOKEN", $token, "User")
Remove-Variable token
```

Then start the server, which reads it from there (with `.venv` activated, or call
`.venv\Scripts\cctv-pipeline-serve`):

```
cctv-pipeline-serve --project-root <dir>
```

Register the MCP adapter with a project `.mcp.json`. It references the token by
environment-variable expansion and does not contain it. An MCP client does not activate
the virtualenv, so give the command's full path:

```json
{
  "mcpServers": {
    "pipeline": {
      "command": "<repo>/.venv/Scripts/cctv-pipeline-mcp",
      "env": { "CCTV_PIPELINE_TOKEN": "${CCTV_PIPELINE_TOKEN}" }
    }
  }
}
```

Never commit the token. Tools are prefixed `pipeline_`. Uploads are CLI-only. The adapter also
serves reference documents as MCP resources, including `cctv-pipeline://references/scene-realism`.

Run the tests with `pytest`.

## Layout

| Path | What lives there |
|---|---|
| `orchestrator/api` | FastAPI app, route table, CLI, generated tool manifest |
| `orchestrator/domain` | Entities and invariants: projects, episodes, shots, scenes, assets |
| `orchestrator/service` | Business logic: scripts, dependencies, sourcing, credits, H3, compose |
| `orchestrator/adapters` | External tools: `blender`, `comfyui`, `minimax_h3`, `assets`, `postprofile`, `timeline` |
| `orchestrator/adapters/qwen` | Local Qwen2.5-Omni review helper, standalone and advisory-only |
| `orchestrator/jobs` | Job queue and journal |
| `orchestrator/store` | On-disk project store |
| `spikes/` | 23 research spikes, each with a contract and findings |
| `tests/` | Contract and conformance tests |

Adapter behaviour is pinned by `*_CONTRACT.md` files kept beside the code they constrain.

## Sample media is not included

The figure asset and the timecode overlay typeface used in development are licensed for internal
evaluation only and are **not cleared for redistribution**. Every rendered frame and clip has
therefore been withheld from this repository and removed from its history. The code, contracts and
findings are complete; only the imagery is absent.

## Licence

Apache-2.0. See [LICENSE](LICENSE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
