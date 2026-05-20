# AI Agent Execution Specification

## Purpose

The AI Agent Panel turns natural-language experiment requests into reviewable, multi-step workflows. The Agent is a coordination layer: it plans and schedules existing application workflows, but it does not send low-level hardware commands and does not bypass current safety checks.

## Planning Flow

1. User sends a natural-language instruction.
2. The Agent collects current software state.
3. The app generates a live Markdown capability brief.
4. The LLM returns a user-visible Markdown reply plus a structured plan.
5. The app parses the plan, rejects unsupported `action_id` values, and merges local blockers into every step.
6. The Agent Panel shows the plan and waits for step-by-step confirmation.

If no API key is configured or the LLM call fails, the local rule planner handles common workflows such as:

- Move to the selected GDS target.
- Move selected stage axes to zero/origin, absolute pulse targets, or relative pulse deltas.
- AutoFocus at the current position.
- AutoFocus, then capture and save one frame.
- Move to the selected GDS target, then capture one frame.
- Run current ImgStitch / stack acquisition settings.
- Associate the latest image with LayoutBond.

## Step Confirmation

The Agent supports three permission modes:

- Conversation-only mode: the Agent can explain and plan, but execution is disabled.
- Authorized step mode: the Agent creates a complete plan, then waits for the user to run each executable step.
- Automatic step mode: the Agent advances through executable steps automatically, while still re-reading state and applying local blockers before every step.

Authorized step mode is the default. A complete plan may contain multiple steps, but each step that changes hardware or experiment state is gated by the selected permission mode. Before every executed step, the app re-reads current state and blocks execution if conditions changed.

Blocking conditions include:

- Motion is busy.
- Keyboard jog motion is busy.
- A position read is pending.
- AutoFocus, FocusMap, or ImgStitch is already running.
- Required serial connection is missing.
- Required camera frame is missing.
- GDS target is not selected.
- GDS-to-stage mapping is not ready.
- No recent image exists for LayoutBond image association.
- FocusMap has no generated mesh.

## Execution Rules

The Agent may dispatch only these high-level application methods:

- `move_gds_mapper_target()` for selected GDS target movement.
- `move_agent_stage()` for Agent-planned stage coordinate movement through the existing stage movement worker.
- `start_autofocus()` for current-position autofocus.
- `capture_agent_single_frame()` for current-frame capture into existing ImgStitch outputs.
- `start_imgstitch()` for current ImgStitch, T-stack, or Z-stack settings.
- `start_af_plane_mapping()` for FocusMap with current mesh/settings.
- `_set_gds_mapper_status()` plus page navigation for latest-image LayoutBond association.
- Existing stop methods for AutoFocus, ImgStitch, and FocusMap stop requests.

The Agent must not call `ControllerSerialClient` directly, build protocol frames, change safety flags to force execution, or write image outputs outside established image workflow paths.

## Result Handling

Image acquisition results remain compatible with existing panels:

- ImgStitch continues writing `last_imgstitch.png` and `imgstitch_session/last_imgstitch.png`.
- Stack workflows continue writing `imgstitch_session/stack_result.png`.
- Agent single-frame capture writes the current frame to `last_imgstitch.png`, `imgstitch_session/last_imgstitch.png`, `imgstitch_session/stack_result.png`, and a timestamped `imgstitch_session/agent_single_frame_*.png`.
- The ImgStitch preview is updated through the existing result queue, so switching back to ImgStitch shows the latest output.

## UI Behavior

The Agent Panel has three columns:

- Left: live microscope view, Stage `X/Y/Z`, Layout `U/V`, Agent phase, model/API/token/status.
- Center: ChatGPT-like conversation with basic Markdown rendering, reset marker, and configurable retained history count.
- Right: task visualization that changes with the active or pending step: the real AutoFocus AF-Z plot when samples exist, ImgStitch/capture preview summary, FocusMap point/plane summary, LayoutBond target/FOV summary, or idle workflow summary.
