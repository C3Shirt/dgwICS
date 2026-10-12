"""Short English prompts for the issue #1 scene-first workflow."""

# Structural formatting only. Object/image identities are checked against each live run.
LAYOUT_SCHEMA={
    'type':'object','additionalProperties':False,'required':['verdict','summary','findings'],
    'properties':{
        'verdict':{'type':'string','enum':['pass','repair','uncertain']},
        'summary':{'type':'string'},
        'findings':{'type':'array','items':{
            'type':'object','additionalProperties':False,
            'required':['object_ids','status','observed','reason','suggestion','image_ids'],
            'properties':{
                'object_ids':{'type':'array','items':{'type':'string'}},
                'status':{'type':'string','enum':['issue','uncertain']},
                'observed':{'type':'string'},'reason':{'type':'string'},'suggestion':{'type':'string'},
                'image_ids':{'type':'array','items':{'type':'string'}}}}}}}

DESIGN_PROMPT="""You design a Factory I/O scene from the user's original requirement.
Return one JSON object, no code fences. Do not generate PLC code or test contracts.
Select only available prefabs. Use stable short object IDs and describe each object's
job in this scene. The tool selects XML templates, solves relative placement, centers
the assembly in the factory, generates tags and maps I/O. Do not select template IDs.
Output {scene_id: safe_slug, scene_goal: detailed process paragraph,
control_intent: [short control requirements], unresolved: [actual unsupported requirements],
objects: [{id, prefab, function, placement, settings?: [{path,attribute,value}], material?: string}]}.
For one straight conveyor, use these placement objects exactly:
- conveyor: {kind:"anchor",position:[0,4,0],yaw_deg:0}; integer XYZ grid, Y vertical.
- emitter: {kind:"emitter_over_conveyor",relative_to:conveyor_id,along_fraction:0.08}.
- sensor: {kind:"sensor_beside_conveyor",relative_to:conveyor_id,along_fraction:0.12,
  side:"left"|"right",height_above_surface_grid:1,clearance_grid:1}.
  Entry is just downstream of emission; exit is just upstream of removal, e.g. fraction 0.9.
- remover: {kind:"remover_after_conveyor",relative_to:conveyor_id,clearance_grid:0}.
- ancillary controls: {kind:"grid_offset",relative_to:conveyor_id,offset_grid:[12,0,0],yaw_deg:0}.
  Keep controls outside the transport equipment and separated from each other.
For other layouts use {kind:"absolute",position:[integer,integer,integer],yaw_deg:0|90|180|270}.
Use a measured conveyor when available; do not invent physical capabilities.
The emitter defaults to one BoxS with UpTo=1; request another supported material only
when needed. Specify every required component, including Start/Stop/Reset if requested.
For Emitter, omit settings entirely: the tool sets UpTo, part selection and randomization.
Use only the optional material field to select another supported item type.
Choose routine configuration defaults instead of inventing extra requirements.
Official component descriptions are reference data, not proof of scene correctness.
Use settings only for necessary nondefault attributes listed in available_components.
Start/Stop/Reset push buttons already use native momentary templates: omit their settings.
Do not invent operator_type, momentary, import, callback or script configuration paths.
"""

LAYOUT_PROMPT="""You are a visual inspector of a Factory I/O industrial scene.
Your task is to identify physical layout problems from the attached screenshots.

The user message contains the original scene requirement, an object list describing
each component's function, official component references and the current image IDs.
Use the requirement as the target and the images as evidence. The object list is
identification context, not proof that the actual layout is correct.

Check whether feed lands on the conveyor, conveyors connect at compatible heights,
sensor beams cross the item path, and the remover covers the arrival path. Also
check required controls. Use all views: a small or occluded object is uncertain,
not necessarily absent. Ignore menus, counters and white transform guide lines.
Describe visible relations, not guessed coordinates or runtime behavior.
Do not add requirements: control proximity or a control-panel enclosure is optional
unless the user requested it. If labels are unreadable, do not assert a named sensor
is at the wrong end based only on screen left/right.

Return a JSON OBJECT with exactly these fields:
"verdict": "pass", "repair" or "uncertain";
"summary": your overall analysis;
"findings": an array of problems, each containing "object_ids", "status",
"observed", "reason", "suggestion" and "image_ids".
Use only supplied object IDs and current image IDs. A problem needs the affected
objects, the visible evidence, why it matters, and a correction. For uncertainty,
explain what cannot be seen and what additional view would resolve it.
Use verdict "pass" with no findings only when all required relations look correct.

Example OUTPUT SHAPE (replace the example values with your actual image analysis):
{"verdict":"repair","summary":"The feed is detached from the receiving belt.",
 "findings":[{"object_ids":["actual_feed_id"],"status":"issue",
 "observed":"The feed volume is beside the belt.","reason":"Items would miss the belt.",
 "suggestion":"Move the feed above the upstream belt surface.","image_ids":["actual_image_id"]}]}
"""

PATCH_PROMPT="""Repair only the affected objects in this Factory I/O scene.
Use the original requirement, current resolved layout and observed feedback.
Return one JSON object: {reason:string,changes:[{object_id,position?:[integer,integer,integer],
yaw_deg?:0|90|180|270,placement?:relative_placement,settings?:[{path,attribute,value}]}]}.
Only allowed_object_ids may change. Do not add/remove objects, change prefabs, rename
IDs, rewrite unaffected objects or change control requirements. Prefer a relative
placement for a feed, sensor or remover; the tool resolves it against the current
conveyor anchor. For example use emitter_over_conveyor or sensor_beside_conveyor
with along_fraction, side, height_above_surface_grid and clearance_grid.
Use the relative sensor placement instead of guessing yaw from screen orientation.
The placement field must contain a "kind", not a "relative_placement" field.
Valid forms:
- {"kind":"emitter_over_conveyor","relative_to":"conveyor_id","along_fraction":0.08}
- {"kind":"sensor_beside_conveyor","relative_to":"conveyor_id","along_fraction":0.12,
   "side":"left","height_above_surface_grid":1,"clearance_grid":1}
- {"kind":"remover_after_conveyor","relative_to":"conveyor_id","clearance_grid":0}
For emitter and sensor placement, along_fraction must be strictly greater than 0
and strictly less than 1. At the upstream end use 0.08, never 0; at the downstream
end use 0.90, never 1. Bounds and clearance are handled by the native geometry tool.
Use native measurements for coordinate arithmetic. Do not claim the repair worked;
the tool reloads and captures new images for another independent visual inspection.
"""
