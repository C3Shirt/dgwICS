"""Replay the same captured scene with one view, multiple views and dynamic checks."""
from pathlib import Path
import time

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
    payload=builder.visual(spec['original_requirement'],design,checks,images)
    payload.update(phase='scene_visual_comparison',comparison_variant='B_dynamic_relations',evidence_scope='archived same-version captures')
    if isinstance(w.raw_model,ModelClient):w.raw_model.config['response_schemas']['vision']=VISUAL_SCHEMA
    before=w.state['model_calls']
    review=checked_call(w,'vision',LAYOUT_PROMPT,payload,lambda v:validate_visual(v,checks,spec,design,images),paths)
    variants['B_dynamic_relations']={'review':review,'input_image_ids':[i['id'] for i in images],'model_calls':w.state['model_calls']-before}
    receipt={**receipt,'manifest_objects':manifest['objects']}
    native=evaluate_plan(checks,design,plan,catalog,registry,receipt=receipt)
    registration=next(r for r in native if r['check_id']=='G-registration')['status']=='satisfied'
    fused=fuse_results(checks,native,review,registration_passed=registration)
    variants['C_geometry_fusion']={'review':fused,'model_calls':0,
        'observation_source':'reuse B exactly, isolating geometry fusion from model randomness'}
    report={'schema_version':1,'source_run':str(source),'source_scene_dir':str(directory),'scene_sha256':manifest['scene_sha256'],'variants':variants,
        'ground_truth_status':'pending independent review','accuracy_metrics':None,
        'limitations':['No detection/false-positive rates without reviewed ground truth.',
                       'This comparison replays captured evidence; it does not reload or repair a scene.'],
        'source_hashes':{str(p.relative_to(source)):file_hash(p) for p in [directory/'requirement-spec.json',directory/'design.json',directory/'scene-plan.json',evidence_path]}}
    write_json(w.output/'comparison.json',report)
    return w.finish('visual_comparison_recorded',phase='visual_comparison',source_run=str(source),
                    comparison_file=str(w.output/'comparison.json'),evidence_scope='archived captured scene',accuracy_metrics=None)
