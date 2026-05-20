# AI Agent Capability And Output Contract

This document is the static template supplied to the external LLM planner. The application also generates a live Markdown capability brief before every LLM call. The live brief is authoritative for current availability, blockers, positions, selected GDS target, API state, and recent outputs.

## Runtime Capability Brief

Before each LLM request, the application sends:

- Current stage controller pulses and physical `X/Y/Z` micrometer coordinates.
- Current mapped GDS `U/V` coordinates when LayoutBond mapping is fitted.
- Selected GDS `U/V` and selected target stage coordinates when available.
- Serial, camera, frame, motion, AutoFocus, FocusMap, and ImgStitch state.
- Current model, API configuration state, current page, and recent image output path.
- A catalog of supported high-level `action_id` values, each with purpose, owning module, prerequisites, hardware effects, confirmation policy, visualization hint, availability, and current blockers.

## Output Contract

The planner must return one JSON object only:

```json
{
  "title": "short plan title",
  "understanding": "plain-language interpretation",
  "reply_markdown": "Markdown shown to the user",
  "needs_clarification": false,
  "visualization_hint": "summary",
  "plan": {
    "steps": [
      {
        "action_id": "autofocus_current_position",
        "title": "Run autofocus",
        "module": "AutoFocus",
        "detail": "Use current AutoFocus settings.",
        "parameters": {},
        "requires_confirmation": true,
        "involves_motion": true,
        "involves_autofocus": true,
        "involves_capture": false,
        "changes_experiment_state": true,
        "visualization_hint": "autofocus",
        "risks": ["Moves Z axis."],
        "blockers": [],
        "recovery_suggestions": []
      }
    ]
  },
  "blockers": [],
  "recovery_suggestions": []
}
```

`reply_markdown` may use headings, lists, bold text, code blocks, and simple tables. Program execution is driven only by `plan.steps[*].action_id`, not by prose.

User-visible `reply_markdown` should be English by default unless the user explicitly asks for another language.

## Supported High-Level Actions

The live capability brief lists availability for these actions:

| `action_id` | Module | Purpose |
| --- | --- | --- |
| `status_summary` | Agent Panel | Explain current state without changing hardware. |
| `move_gds_target` | LayoutBond | Move to the currently selected GDS target through fitted mapping. |
| `stage_move` | Stage Control | Move selected stage axes to zero/origin, absolute pulse targets, or relative pulse deltas without requiring GDS binding. |
| `autofocus_current_position` | AutoFocus | Run the existing Z autofocus workflow. |
| `capture_single_frame` | ImgStitch | Capture the current camera frame and save it to existing ImgStitch output locations. |
| `image_capture_sequence` | ImgStitch | Run current XY stitch, T-stack, or Z-stack settings. |
| `focusmap_current_settings` | FocusMap | Run FocusMap with the current generated mesh and UI settings. |
| `layout_image_overlay` | LayoutBond | Associate the latest saved image with LayoutBond context. |
| `clarify` | Agent Panel | Ask for clarification when a safe mapping is not possible. |

## Safety Boundaries

- The LLM must never output raw serial/controller commands or protocol frames.
- The LLM must never bypass confirmation.
- Hardware or experiment-state-changing steps must set `requires_confirmation` to `true`.
- The app re-checks blockers locally before enabling confirmation and again before each confirmed step executes.
- The Agent may call only existing high-level application workflows.

For `stage_move`, use these parameter shapes:

```json
{"mode": "zero", "axes": ["X", "Y"]}
{"mode": "absolute", "targets": {"X": 0, "Y": 0}}
{"mode": "relative", "deltas": {"X": 100, "Y": -50}}
```

Values are controller pulses unless a future application layer explicitly provides a unit converter.
