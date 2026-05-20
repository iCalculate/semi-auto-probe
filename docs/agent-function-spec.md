# AI Agent Function Specification

This document is the function catalog supplied to the external LLM planner. The planner may choose one supported high-level action and produce a user-reviewable plan. The application, not the LLM, performs final safety checks and execution.

## Output Contract

The planner must return one JSON object with:

- `action`: one of `move_gds_target`, `autofocus_current_position`, `image_capture_sequence`, `layout_image_overlay`, `clarify`
- `title`: short plan title
- `understanding`: plain-language interpretation of the user request
- `steps`: ordered list of plan steps with `title`, `module`, `detail`, `involves_motion`, `involves_autofocus`, `involves_capture`
- `requires_confirmation`: true for any operation that changes experiment state
- `involves_motion`, `involves_autofocus`, `involves_capture`: plan-level flags
- `risks`: user-visible risk notes
- `blockers`: known reasons the plan should not run
- `recovery_suggestions`: short recovery guidance

## Available Actions

## Coordinate Semantics

- Controller coordinates are raw motor pulses.
- Stage physical coordinates are derived micrometers from controller pulses and motor configuration.
- GDS coordinates are layout `u/v` coordinates.
- The current stage has no current GDS coordinate until a GDS-to-stage binding has been fitted.
- A selected GDS point can exist before binding, but it has no executable stage target until the binding is ready.

### `move_gds_target`

Purpose: Move the XY stage to the currently selected GDS/LayoutBond target.

Existing app entrypoint:

- `ProbeApp.move_gds_mapper_target(target_x_um, target_y_um)`

Required context:

- Serial connected
- No active motion, AutoFocus, FocusMap, or ImgStitch
- GDS target selected
- GDS-to-stage binding ready

Safety:

- Requires confirmation
- Involves XY motion
- Must not construct serial commands directly

### `autofocus_current_position`

Purpose: Run the existing Z-axis autofocus workflow at the current XY position.

Existing app entrypoint:

- `ProbeApp.start_autofocus()`

Required context:

- Serial connected
- Camera running with a current frame
- No active motion, AutoFocus, FocusMap, or ImgStitch

Safety:

- Requires confirmation
- Involves Z motion
- Uses current AutoFocus UI settings

### `image_capture_sequence`

Purpose: Run the current ImgStitch or image-stack acquisition settings.

Existing app entrypoint:

- `ProbeApp.start_imgstitch()`

Required context:

- Serial connected
- Camera running with a current frame
- No active motion, AutoFocus, FocusMap, or ImgStitch
- Current ImgStitch or stack settings are valid

Safety:

- Requires confirmation
- May involve XY and Z motion
- May involve autofocus if current acquisition settings enable focus sampling
- Uses current ImgStitch UI settings

### `layout_image_overlay`

Purpose: Associate the latest stitched image output with the current LayoutBond context for inspection.

Existing app behavior:

- Set LayoutBond status text with the latest image path
- Navigate to the LayoutBond page

Required context:

- GDS-to-stage binding ready
- Recent stitched image exists
- No active workflow

Safety:

- Requires confirmation because it changes the current inspection context
- Does not perform pixel-level image registration in v1
- Does not move hardware

### `clarify`

Purpose: Ask the user for a clearer or supported task.

Use when:

- The request does not map to a supported action
- The request asks for an unsupported hardware operation
- The request is ambiguous enough that choosing an action would be unsafe

Safety:

- Does not require confirmation
- Does not execute anything

## Hard Boundaries

- The LLM must never send or propose raw serial/controller commands.
- The LLM must never bypass confirmation.
- The LLM must not claim that an operation has already executed.
- The LLM may describe only high-level app workflows listed above.
- The application will recompute blockers locally before enabling confirmation and again before execution.
