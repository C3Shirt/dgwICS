"""Replay the same captured scene with one view, multiple views and dynamic checks."""
from pathlib import Path
import time
from copy import deepcopy

from .contracts import require, canonical_hash, file_hash, read_json, write_json
from .scene_visual import VISUAL_SCHEMA, image_context, validate_visual
from .scene_context import PromptContextBuilder
from .scene_layout import PLACEMENTS, checked_call
from .scene_verifier import default_registry, compile_check_plan, evaluate_plan, fuse_results
from .simple_prompts import LEGACY_LAYOUT_PROMPT, LAYOUT_SCHEMA, LAYOUT_PROMPT


def compare_captures(w, source_run, single_view='iso_a',revision=None):
    from .simple import validate_review
    from .llm import ModelClient
    w.started=time.monotonic();w.output.mkdir(parents=True,exist_ok=True)
    w.training.source_kind='visual_benchmark'  # Cannot be exported as an automatic training gold.
    source=Path(source_run).resolve();state=read_json(source/'run.json')
    require(state.get('framework')=='simple','comparison needs a simple-framework source')
    require(revision is None or type(revision) is int and revision>=0,'invalid comparison revision')
    directory=(source/('scene-'+str(revision).zfill(3))).resolve() if revision is not None else Path(state['scene_dir']).resolve()
    require(directory.is_relative_to(source),'source scene is outside its run')
    spec=read_json(directory/'requirement-spec.json');design=read_json(directory/'design.json');plan=read_json(directory/'scene-plan.json')
    require(canonical_hash(spec)==state['requirements_sha256'],'independent source requirements changed')
    if directory==Path(state['scene_dir']).resolve():
        require(canonical_hash(design)==state['design_sha256'] and canonical_hash(plan)==state['plan_sha256'],'source scene contracts changed')
    catalog=read_json(source/'catalog.json');manifest=read_json(directory/'manifest.json');receipt=read_json(directory/'load-receipt.json')
    evidence_path=directory/'visual/combined-evidence.json'
    if not evidence_path.is_file():evidence_path=directory/'visual/evidence.json'
    evidence=read_json(evidence_path);paths=[Path(r['path']) for r in evidence['records'] if r['kind']=='image']
    rendered={'manifest':manifest,'evidence':evidence,'images':paths};images=image_context(rendered,directory)
    registry=default_registry();checks=compile_check_plan(spec,design,registry)
    builder=PromptContextBuilder(catalog,spec,PLACEMENTS)
    objects={o['id'] for o in design['objects']};variants={}
    for name,subset in (('A_single_view',[i for i in images if i['view_id']==single_view][:1]),('A_multi_view',images)):
        require(subset,'requested single view unavailable')
        selected={r['id'] for r in subset};selected_paths=[Path(r['path']) for r in evidence['records'] if r.get('id') in selected]
        from .component_kb import knowledge_base
        payload={'phase':'scene_visual_comparison','requirement':spec['original_requirement'],
            'objects':[{k:o[k] for k in ('id','prefab','function')} for o in design['objects']],
            'component_reference':knowledge_base().references([o['prefab'] for o in design['objects']],'vision'),
            'evidence':{'records':subset},'evidence_scope':'archived same-version captures; no current software execution claim',
            'comparison_variant':name}
        if isinstance(w.raw_model,ModelClient):w.raw_model.config['response_schemas']['vision']=LAYOUT_SCHEMA
        before=w.state['model_calls']
        review=checked_call(w,'vision',LEGACY_LAYOUT_PROMPT,payload,lambda v:validate_review(v,objects,selected),selected_paths)
        variants[name]={'review':review,'input_image_ids':[i['id'] for i in subset],'model_calls':w.state['model_calls']-before}
    receipt={**receipt,'manifest_objects':manifest['objects']}
    for name,spatial in (('B_rich_visual_only',False),('C_rich_geometry_assisted',True)):
        payload=builder.visual(spec['original_requirement'],design,checks,images,plan,receipt,spatial_context=spatial)
        write_json(w.output/(name+'-input.json'),payload)
        if isinstance(w.raw_model,ModelClient):w.raw_model.config['response_schemas']['vision']=VISUAL_SCHEMA
        before=w.state['model_calls']
        review=checked_call(w,'vision',LAYOUT_PROMPT,payload,lambda v:validate_visual(v,checks,spec,design,images),paths)
        write_json(w.output/(name+'-response.json'),review['raw_output'])
        variants[name]={'review':review,'input_image_ids':[i['id'] for i in images],'model_calls':w.state['model_calls']-before,
                        'inspection_mode':payload['inspection_mode'],'output_contract':'issues_only_v2'}
    native=evaluate_plan(checks,design,plan,catalog,registry,receipt=receipt)
    registration=next(r for r in native if r['check_id']=='G-registration')['status']=='satisfied'
    fused=fuse_results(checks,native,review,registration_passed=registration)
    variants['D_geometry_fusion']={'review':fused,'model_calls':0,
        'observation_source':'reuse C exactly, isolating geometry fusion from model randomness'}
    report={'schema_version':1,'source_run':str(source),'source_scene_dir':str(directory),'scene_sha256':manifest['scene_sha256'],'variants':variants,
        'ground_truth_status':'pending independent review','accuracy_metrics':None,
        'limitations':['No detection/false-positive rates without reviewed ground truth.',
                       'A retains the legacy prompt/output contract; A versus B/C is a qualitative comparison, not a controlled context-only ablation.',
                       'B and C use the same images, system prompt and output schema; they differ only in auxiliary spatial context.',
                       'This comparison replays captured evidence; it does not reload or repair a scene.'],
        'source_hashes':{str(p.relative_to(source)):file_hash(p) for p in [directory/'requirement-spec.json',directory/'design.json',directory/'scene-plan.json',evidence_path]}}
    write_json(w.output/'comparison.json',report)
    return w.finish('visual_comparison_recorded',phase='visual_comparison',source_run=str(source),
                    comparison_file=str(w.output/'comparison.json'),evidence_scope='archived captured scene',accuracy_metrics=None)


def probe_vision_protocol(w, candidate_path):
    """A bounded transport/output-format probe through the framework's recorder.

    Reuse the exact saved system/user content and image bytes. No scene tools,
    layout verdict or training gold is produced by this diagnostic experiment.
    """
    from .llm import ModelClient
    w.started=time.monotonic();w.output.mkdir(parents=True,exist_ok=True)
    require(isinstance(w.raw_model,ModelClient),'protocol probe requires the configured real model client')
    w.training.source_kind='protocol_probe'
    source=Path(candidate_path).resolve();candidate=read_json(source)
    require(candidate.get('role')=='vision' and source.parent.name=='candidates','probe needs a saved vision candidate')
    systems=[m['content'] for m in candidate['messages'] if m['role']=='system'];require(len(systems)==1,'missing original system prompt')
    payload=candidate['payload'];images=[]
    for image in candidate['images']:
        path=(source.parents[1]/image['path']).resolve()
        require(path.is_relative_to(source.parents[1]) and file_hash(path)==image['sha256'],'probe image identity changed')
        images.append(path)
    require(images,'protocol probe needs the original images')
    fmt=candidate.get('model_metadata',{}).get('response_format') or {}
    full_schema=fmt.get('json_schema',{}).get('schema')
    require(full_schema and full_schema.get('type')=='object','source candidate needs the original full object schema')
    # Only root structure is constrained here; nested layout validity is not
    # evaluated by this probe. The source prompt/output_contract is unchanged.
    simple_schema={k:deepcopy(full_schema[k]) for k in ('type','required','additionalProperties') if k in full_schema}
    simple_schema['properties']={k:{'type':deepcopy(v['type'])} for k,v in full_schema['properties'].items()}
    for prop in simple_schema['properties'].values():
        if prop['type']=='array':prop['items']={'type':'object','additionalProperties':True}
    config=w.raw_model.config;saved=deepcopy(config);trials=[]
    try:
        for streaming in (True,False):
            for name,schema in (('json_object',None),('simple_schema',simple_schema),('full_schema',full_schema)):
                config['stream']=streaming;config['json_mode']=True
                config['schema_roles']=[r for r in saved.get('schema_roles',[]) if r!='vision']
                config['response_schemas']=deepcopy(saved.get('response_schemas',{}));config['response_schemas'].pop('vision',None)
                if schema is not None:config['response_schemas']['vision']=schema
                before=set((w.output/'training-data/candidates').glob('*.json'))
                error=None;accepted=False
                w.trace('protocol_probe_trial',active_role='vision',streaming=streaming,response_format=name)
                try:
                    value=w.call('vision',systems[0],deepcopy(payload),images);accepted=isinstance(value,dict)
                except Exception as exc:error=str(exc)
                new=set((w.output/'training-data/candidates').glob('*.json'))-before
                require(len(new)==1,'protocol trial did not produce exactly one recorded candidate')
                path=next(iter(new));record=read_json(path)
                trials.append({'stream':streaming,'format':name,'json_object_received':accepted,'error':error,
                    'response_text':record['response_text'],'candidate':str(path.relative_to(w.output)),
                    'metadata':record.get('model_metadata',{})})
    finally:config.clear();config.update(saved)
    report={'source_candidate':str(source),'frozen_input_sha256':canonical_hash({'system':systems[0],'payload':payload,
            'images':[i['sha256'] for i in candidate['images']]}),'trials':trials,
            'scope':'protocol shape diagnostics only; no layout acceptance or causal claim from one trial per combination'}
    write_json(w.output/'protocol-probe.json',report)
    return w.finish('protocol_probe_recorded',phase='vision_protocol_probe',report_file=str(w.output/'protocol-probe.json'),
                    accepted_object_trials=sum(t['json_object_received'] for t in trials),total_trials=len(trials))
