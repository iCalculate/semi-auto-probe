# AI Agent Execution Specification

## Purpose

The AI Agent Panel turns natural language requests into reviewable operation plans. It does not send low-level hardware commands and does not bypass existing application workflows. Hardware execution remains owned by the current motion, autofocus, image acquisition, and LayoutBond modules.

## Supported v1 Tasks

| User intent | Agent action | Existing module used |
| --- | --- | --- |
| Move to the selected GDS point | `move_gds_target` | `LayoutBond` / `GDSStageMapperPanel` |
| Autofocus at the current position | `autofocus_current_position` | `AutoFocus` |
| Run the current image acquisition sequence | `image_capture_sequence` | `ImgStitch` / image stack workflow |
| Associate the latest image with the layout | `layout_image_overlay` | `LayoutBond` status context |

The Agent uses `GDS` terminology consistently in prompts, UI labels, and docs.

## Planning Rules

The v1 planner is LLM-first when API configuration is present. It reads `docs/agent-function-spec.md`, the current software state, and the user's natural-language instruction, then returns a controlled JSON operation plan. If no API key is configured, or if the API call fails, the app falls back to the local rule-based planner for the four core tasks.

Each plan includes:

- The interpreted task.
- The existing module that will be called.
- Ordered execution steps.
- Whether motion, autofocus, or image capture is involved.
- Required confirmation state.
- Risks, blockers, and recovery suggestions.

Plan generation is read-only. It must not move hardware, start autofocus, capture images, change configuration, or write controller commands.

## API Configuration

The external planner uses an OpenAI-compatible chat-completions API. Configure it from the app `Config` page under `AI Agent API`; values are saved in the local `probe_config.local.json`.

Defaults follow DeepSeek's official OpenAI-compatible setup:

| Field | Default |
| --- | --- |
| Provider | DeepSeek / OpenAI-compatible |
| Base URL | `https://api.deepseek.com` |
| Model | `deepseek-chat` |
| Timeout | `30` seconds |

Environment variables can still override or supply values when the config API key is empty:

| Variable | Meaning |
| --- | --- |
| `SEMI_AUTO_PROBE_AGENT_API_KEY` | Required to enable LLM planning |
| `SEMI_AUTO_PROBE_AGENT_MODEL` | Model name; defaults to `deepseek-chat` |
| `SEMI_AUTO_PROBE_AGENT_BASE_URL` | API base URL; defaults to `https://api.deepseek.com` |
| `SEMI_AUTO_PROBE_AGENT_TIMEOUT_SECONDS` | Request timeout; defaults to `30` |

Without an API key in Config or `SEMI_AUTO_PROBE_AGENT_API_KEY`, the Agent Panel still works in local rule-fallback mode.

## Confirmation Rules

The following actions always require user confirmation before execution:

- Any XY or Z stage movement.
- AutoFocus.
- ImgStitch, Z-stack, T-stack, or other image acquisition workflows.
- Any operation that changes the current experiment state.
- Layout/image association in the Agent Panel.

The Agent Panel enables execution only when the plan is executable and waiting for confirmation.

## Safety Gates

Before a confirmed plan starts, the application checks the current state again. Execution is blocked when any relevant condition is true:

- Motion is busy.
- Keyboard jog motion is busy.
- A position read is pending.
- AutoFocus is already running.
- FocusMap is already running.
- ImgStitch is already running.
- Required serial connection is missing.
- Required camera frame is missing.
- GDS target is not selected.
- GDS-to-stage binding is not ready.
- No recent stitched image exists for layout association.

These checks are repeated at execution time because the state can change between planning and user confirmation.

## Execution Rules

The Agent may call only these existing high-level application methods in v1:

- `move_gds_mapper_target()` for GDS target movement.
- `start_autofocus()` for autofocus.
- `start_imgstitch()` for the current acquisition sequence.
- `_set_gds_mapper_status()` and page navigation for latest-image association.
- `stop_autofocus()`, `stop_imgstitch()`, and `stop_af_plane_mapping()` for Agent stop requests.

The Agent must not call `ControllerSerialClient` directly, build protocol frames, or alter safety flags to force execution.

## Progress And Recovery

During execution, the Agent Panel mirrors status updates from the active workflow:

- LayoutBond move status and completion/failure.
- AutoFocus progress, stop, and failure.
- ImgStitch progress, save path, stop, and failure.
- FocusMap stop status when applicable.

If execution is blocked or fails, the Agent Panel shows the blocker or error and keeps the existing manual controls available for recovery.

## v1 Limitation

The latest-image layout action associates the most recent stitched output with the current LayoutBond context for inspection. It is not a pixel-registered image overlay. A real image-to-layout overlay requires a separate calibration and transform design.
