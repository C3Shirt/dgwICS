"""Bounded spatial checks from inspected native prefab/instance measurements.

These necessary connection checks do not prove collisions, item transport or the
complete process. Models still choose placements; this tool never moves objects.
"""
from copy import deepcopy
import itertools
import math

from .contracts import require, canonical_hash


def rotate(vector, yaw):
    x,y,z=vector
    return ([x,y,z],[z,y,-x],[-x,y,-z],[-z,y,x])[int(yaw//90)%4]


def relative_layout_capability(task, catalog):
    """Select the supported solver from inspected component capabilities, never prefab names."""
    native=catalog.get('native_geometry',{}).get('prefabs',{})
    prefabs={o['prefab'] for o in task['objects']}
    measured=[o['id'] for o in task['objects'] if 'conveyor' in
              native.get(o['prefab'],{}).get('component_geometry_local',{})]
    unmeasured=[o['id'] for o in task['objects'] if 'conveyor' in o['prefab'].lower() and o['id'] not in measured]
    supported=len(measured)==1 and not unmeasured
    return {'supported':supported,'measured_conveyor_ids':measured,'unmeasured_conveyor_ids':unmeasured,
            'fallback':'absolute integer XYZ/cardinal yaw model layout plus fresh visual and physical acceptance',
            'missing_conveyor_semantics':'bounds alone never prove flow direction or conveying surface'}


def center_scene_layout(value, catalog):
    """Translate the whole assembly using measured factory bounds; preserve relative positions."""
    plan=deepcopy(value);native=catalog.get('native_geometry') or {}
    world=native.get('world_bounds')
    if not world:return plan,None
    scale=native['coordinate_system']['grid_metres']
    require(type(scale) in (int,float) and scale>0,'invalid measured grid scale')
    require(all(isinstance(world.get(k),list) and len(world[k])==3 and
                all(type(v) in (int,float) and math.isfinite(v) for v in world[k]) for k in ('min','max')),
            'invalid measured factory bounds')
    require(all(a<b for a,b in zip(world['min'],world['max'])),'invalid measured factory extent')
    templates={t['template_id']:t for t in catalog['templates']}
    def bounds():
        rows=[]
        for obj in plan['objects']:
            prefab=templates[obj['template_id']]['prefab']
            require(prefab in native['prefabs'],'centering needs measured bounds for '+prefab)
            rows.append(project_object(obj,prefab,native['prefabs'][prefab],scale)['bounds_world'])
        require(bool(rows),'centering needs at least one object')
        return {k:[fn(r[k][axis] for r in rows) for axis in range(3)] for k,fn in (('min',min),('max',max))}
    original=bounds();target=[(a+b)/2 for a,b in zip(world['min'],world['max'])]
    center=[(a+b)/2 for a,b in zip(original['min'],original['max'])]
    delta=[round((target[a]-center[a])/scale) if a in (0,2) else
           max(0,math.ceil((world['min'][1]-original['min'][1])/scale-1e-7)) for a in range(3)]
    for obj in plan['objects']:obj['position']=[v+d for v,d in zip(obj['position'],delta)]
    result=bounds()
    require(all(result['min'][a]>=world['min'][a]-1e-7 and result['max'][a]<=world['max'][a]+1e-7
                for a in range(3)),'scene does not fit inside measured factory bounds after centering')
    return plan,{'authority':'measured_factory_bounds','translation_grid':delta,'world_bounds':world,
        'original_bounds_world':original,'result_bounds_world':result,'target_center_world':target,
        'result_center_world':[(a+b)/2 for a,b in zip(result['min'],result['max'])],
        'relative_positions_preserved':True,'physical_acceptance_passed':False}


def resolve_transport_clearance(value, catalog):
    """Resolve conservative single-conveyor mounting clearance from a model's absolute layout.

    Preserve model topology, cardinal orientation, sensor XZ and chosen discharge side. This does
    not measure conveyor flow or an exact carrying surface, and cannot grant physical acceptance.
    """
    plan=deepcopy(value);native=catalog.get('native_geometry')
    changes=[]
    if not native:return plan,changes
    templates={t['template_id']:t for t in catalog['templates']};scale=native['coordinate_system']['grid_metres']
    belts=[o for o in plan['objects'] if 'conveyor' in templates[o['template_id']]['prefab'].lower()]
    if len(belts)!=1:return plan,changes
    belt=belts[0];prefab=templates[belt['template_id']]['prefab']
    projected=project_object(belt,prefab,native['prefabs'][prefab],scale);bounds=projected['bounds_world']
    top=bounds['max'][1];axis=max((0,2),key=lambda i:bounds['max'][i]-bounds['min'][i])
    for obj in plan['objects']:
        if obj is belt:continue
        prefab=templates[obj['template_id']]['prefab'];metric=native['prefabs'][prefab]
        component=metric['component_geometry_local'];before=deepcopy(obj['position'])
        if 'emitter' in component or prefab=='Remover':
            corners=[rotate(v,obj['yaw_deg']) for v in itertools.product(*zip(
                metric['bounds_local_metres']['min'],metric['bounds_local_metres']['max']))]
            lower=min(v[1] for v in corners)
            obj['position'][1]=max(obj['position'][1],int(math.ceil((top-lower)/scale-.5-1e-7)))
            if prefab=='Remover':
                # The model chose this side; do not infer a positive/native flow direction.
                pivot=(obj['position'][axis]+.5)*scale
                centre=(bounds['min'][axis]+bounds['max'][axis])/2
                if pivot>=centre:
                    minimum=bounds['max'][axis]-min(v[axis] for v in corners)
                    obj['position'][axis]=max(obj['position'][axis],int(math.ceil(minimum/scale-.5-1e-7)))
                else:
                    maximum=bounds['min'][axis]-max(v[axis] for v in corners)
                    obj['position'][axis]=min(obj['position'][axis],int(math.floor(maximum/scale-.5+1e-7)))
        elif 'sensor' in component:
            origin=rotate(component['sensor']['origin'],obj['yaw_deg'])
            obj['position'][1]=max(obj['position'][1],int(math.ceil((top+scale-origin[1])/scale-.5-1e-7)))
        if before!=obj['position']:
            changes.append({'object_id':obj['id'],'before':before,'after':deepcopy(obj['position']),
                'basis':'conservative native voxel bounds; model XZ topology/orientation/discharge side preserved'})
    return plan,changes


def project_object(obj, prefab, metric, scale):
    pivot=[(v+0.5)*scale for v in obj['position']]
    def point(v): return [a+b for a,b in zip(pivot,rotate(v,obj['yaw_deg']))]
    bounds=metric['bounds_local_metres']
    corners=[point(v) for v in itertools.product(*zip(bounds['min'],bounds['max']))]
    context=deepcopy(metric['component_geometry_local'])
    for value in context.values():
        if 'origin' in value: value['origin']=point(value['origin'])
        if 'direction' in value: value['direction']=rotate(value['direction'],obj['yaw_deg'])
        if 'surface_bounds' in value:
            b=value['surface_bounds'];cs=[point(v) for v in itertools.product(*zip(b['min'],b['max']))]
            value['surface_bounds']={'min':[min(v[i] for v in cs) for i in range(3)],
                                     'max':[max(v[i] for v in cs) for i in range(3)]}
    return {'id':obj['id'],'prefab':prefab,'bounds_world':{'min':[min(v[i] for v in corners) for i in range(3)],
                                                       'max':[max(v[i] for v in corners) for i in range(3)]},
            'component_geometry':context}


def resolve_relative_layout(value,catalog, *, verify=True):
    """Model selects topology/relative positions; solve only native coordinate arithmetic."""
    native=catalog.get('native_geometry')
    require(native is not None,'relative placement requires native prefab measurements')
    scale=native['coordinate_system']['grid_metres']
    require(scale==.125 and native['coordinate_system']['vertical_axis']=='Y','unsupported native grid')
    templates={t['template_id']:t for t in catalog['templates']}
    objects=value['objects']
    require(all(set(o)=={'id','template_id','placement'} and isinstance(o.get('placement'),dict) for o in objects),
            'constraint objects need only id, template_id and an object placement')
    conveyors=[o for o in objects if 'conveyor' in
        native['prefabs'][templates[o['template_id']]['prefab']]['component_geometry_local']]
    require(len(conveyors)==1,'relative placement tool requires exactly one conveyor with measured flow/surface geometry')
    belt=conveyors[0];p=belt.get('placement',{})
    require(set(p)=={'kind','position','yaw_deg'} and p['kind']=='anchor','conveyor placement requires anchor, position and yaw_deg')
    require(isinstance(p['position'],list) and len(p['position'])==3 and all(type(v) is int for v in p['position']),
            'anchor position must be integer XYZ')
    require(type(p['yaw_deg']) in (int,float) and p['yaw_deg']%90==0,'anchor yaw must be cardinal')
    resolved={belt['id']:{'id':belt['id'],'template_id':belt['template_id'],'position':p['position'],'yaw_deg':p['yaw_deg']}}
    prefab=templates[belt['template_id']]['prefab'];metric=native['prefabs'][prefab]
    measured=project_object(resolved[belt['id']],prefab,metric,scale)
    conveyor=measured['component_geometry']['conveyor'];bounds=measured['bounds_world']
    direction=conveyor['direction'];axis=max((0,2),key=lambda i:abs(direction[i]))
    require(abs(direction[axis])>.999 and abs(direction[1])<1e-5,'relative placement needs a horizontal cardinal flow')
    surface=conveyor.get('surface_bounds',bounds)['max'][1]
    def grid(point):return [int(round(v/scale-.5)) for v in point]
    def fraction_point(f):
        require(type(f) in (int,float) and math.isfinite(f) and 0<f<1,'along_fraction must lie strictly between 0 and 1')
        point=[(a+b)/2 for a,b in zip(bounds['min'],bounds['max'])]
        point[axis]=(bounds['min'][axis]+f*(bounds['max'][axis]-bounds['min'][axis]) if direction[axis]>0 else
                     bounds['max'][axis]-f*(bounds['max'][axis]-bounds['min'][axis]))
        return point
    for obj in objects:
        if obj['id']==belt['id']:continue
        require(set(obj)=={'id','template_id','placement'},'relative object must contain only id, template_id, placement')
        p=obj['placement'];require(isinstance(p,dict) and p.get('relative_to')==belt['id'],'placement must reference the selected conveyor ID')
        prefab=templates[obj['template_id']]['prefab'];metric=native['prefabs'][prefab];component=metric['component_geometry_local']
        kind=p.get('kind');yaw=resolved[belt['id']]['yaw_deg']
        if kind=='emitter_over_conveyor':
            require(set(p)=={'kind','relative_to','along_fraction'} and 'emitter' in component,'emitter placement fields/type invalid')
            target=fraction_point(p['along_fraction']);offset=rotate(component['emitter']['origin'],yaw)
            pos=grid([a-b for a,b in zip(target,offset)])
            # Lowest emitter occupied cell stays above the measured roller surface.
            pos[1]=int(math.ceil((surface-metric['bounds_local_metres']['min'][1])/scale-.5-1e-7))
        elif kind=='sensor_beside_conveyor':
            require(set(p)=={'kind','relative_to','along_fraction','side','height_above_surface_grid','clearance_grid'} and
                    'sensor' in component,'sensor placement fields/type invalid')
            require(p['side'] in ('left','right'),'sensor side must be left or right of positive conveying direction')
            require(type(p['height_above_surface_grid']) is int and 1<=p['height_above_surface_grid']<=8,
                    'sensor height_above_surface_grid must be integer 1..8')
            require(type(p['clearance_grid']) is int and 1<=p['clearance_grid']<=4,'sensor clearance_grid must be integer 1..4')
            outward=[-direction[2],0,direction[0]]
            if p['side']=='right':outward=[-v for v in outward]
            side_axis=2 if axis==0 else 0
            target=fraction_point(p['along_fraction'])
            target[side_axis]=(bounds['max'][side_axis] if outward[side_axis]>0 else bounds['min'][side_axis])+outward[side_axis]*p['clearance_grid']*scale
            target[1]=surface+p['height_above_surface_grid']*scale
            yaws=[i*90 for i in range(4) if sum(a*b for a,b in zip(rotate(component['sensor']['direction'],i*90),[-v for v in outward]))>.999]
            require(len(yaws)==1,'native sensor beam cannot face the requested conveyor side');yaw=yaws[0]
            offset=rotate(component['sensor']['origin'],yaw);pos=grid([a-b for a,b in zip(target,offset)])
            pos[1]=int(math.ceil((target[1]-offset[1])/scale-.5-1e-7))
        elif kind=='remover_after_conveyor':
            require(set(p)=={'kind','relative_to','clearance_grid'} and prefab=='Remover','remover placement fields/type invalid')
            require(type(p['clearance_grid']) is int and 0<=p['clearance_grid']<=4,'remover clearance_grid must be integer 0..4')
            local=metric['bounds_local_metres'];corners=[rotate(v,yaw) for v in itertools.product(*zip(local['min'],local['max']))]
            target=[(a+b)/2 for a,b in zip(bounds['min'],bounds['max'])]
            if direction[axis]>0:
                target[axis]=bounds['max'][axis]-min(v[axis] for v in corners)+p['clearance_grid']*scale
            else:
                target[axis]=bounds['min'][axis]-max(v[axis] for v in corners)-p['clearance_grid']*scale
            pos=grid(target)
            # Round outward so the removal volume never moves back inside the belt.
            pos[axis]=int(math.ceil(target[axis]/scale-.5-1e-7) if direction[axis]>0 else math.floor(target[axis]/scale-.5+1e-7))
            pos[1]=int(math.ceil((surface-local['min'][1])/scale-.5-1e-7))
        elif kind=='grid_offset':
            require(set(p)=={'kind','relative_to','offset_grid','yaw_deg'} and prefab!='Remover' and not any(k in component for k in ('emitter','sensor','conveyor')),
                    'grid_offset is for ancillary controls/supports, not feed or detection geometry')
            delta=p['offset_grid'];require(isinstance(delta,list) and len(delta)==3 and all(type(v) is int for v in delta),'offset_grid needs integer XYZ')
            pos=[a+b for a,b in zip(resolved[belt['id']]['position'],delta)];yaw=p['yaw_deg']
            require(type(yaw) in (int,float) and yaw%90==0,'offset object yaw must be cardinal')
        else:require(False,'unsupported relative placement kind: '+str(kind))
        resolved[obj['id']]={'id':obj['id'],'template_id':obj['template_id'],'position':pos,'yaw_deg':yaw}
    plan={**{k:deepcopy(v) for k,v in value.items() if k!='objects'},'objects':[resolved[o['id']] for o in objects]}
    if verify:
        plan,_=center_scene_layout(plan,catalog)
        validate_layout_geometry(plan,catalog)
    return plan


def ray_crosses_footprint(origin,direction,range_,bounds):
    lower,upper=0.0,range_
    for axis in (0,2):
        if abs(direction[axis])<1e-8:
            if not bounds['min'][axis]<=origin[axis]<=bounds['max'][axis]: return False
        else:
            a=(bounds['min'][axis]-origin[axis])/direction[axis];b=(bounds['max'][axis]-origin[axis])/direction[axis]
            lower=max(lower,min(a,b));upper=min(upper,max(a,b))
    return lower<=upper


def connection_checks(objects):
    conveyors=[o for o in objects if 'conveyor' in o.get('component_geometry',{})]
    emitters=[o for o in objects if 'emitter' in o.get('component_geometry',{})]
    missing=[o for o in objects if 'conveyor' in o.get('prefab','').lower() and o not in conveyors]
    if missing:
        return {'passed':False,'scope':'missing_conveyor_geometry','spatial_checks_applied':False,
            'reason':'Native conveyor flow/surface geometry is missing; object bounds are insufficient to verify transport connections.',
            'issues':[{'object_id':o['id'],'target':'tools','error':'native conveyor component geometry missing',
                       'prefab':o.get('prefab')} for o in missing],'measurements':objects}
    if len(conveyors)!=1 or len(emitters)!=1:
        return {'passed':False,'scope':'unsupported_topology','spatial_checks_applied':False,'issues':[],
            'reason':'No applicable spatial verifier: the current connection checker needs one measured conveyor and one emitter.',
            'measured_conveyor_count':len(conveyors),'measured_emitter_count':len(emitters),'measurements':objects}
    belt=conveyors[0];bounds=belt['bounds_world'];issues=[]
    feed=emitters[0];origin=feed['component_geometry']['emitter']['origin']
    if any(not bounds['min'][axis]<=origin[axis]<=bounds['max'][axis] for axis in (0,2)):
        issues.append({'object_id':feed['id'],'error':'emission origin is outside conveyor XZ footprint',
                       'origin_world':origin,'conveyor_id':belt['id'],'conveyor_bounds_world':bounds})
    for sensor in (o for o in objects if 'sensor' in o.get('component_geometry',{})):
        ray=sensor['component_geometry']['sensor']
        if not ray_crosses_footprint(ray['origin'],ray['direction'],ray['range_metres'],bounds):
            issues.append({'object_id':sensor['id'],'error':'sensor ray cannot reach conveyor XZ footprint',
                           'ray_world':ray,'conveyor_id':belt['id'],'conveyor_bounds_world':bounds})
    return {'passed':not issues,'scope':'single_conveyor_spatial_connections','spatial_checks_applied':True,
            'issues':issues,'limitations':['XZ reachability only; no physical transport or timing acceptance'],
            'measurements':objects}


def validate_layout_geometry(plan,catalog):
    native=catalog.get('native_geometry')
    if native is None: return
    system=native['coordinate_system']
    require(system['vertical_axis']=='Y' and system['axis_order']==['X','Y','Z'] and system['grid_metres']==0.125,
            'unsupported native coordinate system')
    templates={t['template_id']:t for t in catalog['templates']}
    projected=[]
    for obj in plan['objects']:
        prefab=templates[obj['template_id']]['prefab']
        require(prefab in native['prefabs'],'native geometry unavailable for selected prefab '+prefab)
        projected.append(project_object(obj,prefab,native['prefabs'][prefab],system['grid_metres']))
    # A conservative clearance check for transport source/sink volumes. No object is moved.
    for conveyor in (o for o in projected if 'conveyor' in o['prefab'].lower()):
        a=conveyor['bounds_world']
        for other in (o for o in projected if 'emitter' in o['component_geometry'] or o['prefab']=='Remover'):
            b=other['bounds_world']
            overlap=[min(a['max'][i],b['max'][i])-max(a['min'][i],b['min'][i]) for i in range(3)]
            if all(v>1e-6 for v in overlap):
                offsets={'negative':[math.floor((a['min'][i]-b['max'][i])/.125+1e-7) for i in range(3)],
                         'positive':[math.ceil((a['max'][i]-b['min'][i])/.125-1e-7) for i in range(3)]}
                require(False,'transport clearance rejected: '+str({'object_id':other['id'],'conveyor_id':conveyor['id'],
                    'object_bounds':b,'conveyor_bounds':a,'intersection_metres':overlap,
                    'one_axis_separation_options_grid':offsets,
                    'instruction':'Model must choose a physically suitable single-axis separation; preserve flow/sensor relations. '
                        'Emitter must be above the carrying surface; removal volume must be downstream, not inside the conveyor.',
                    'scope':'conservative full-volume clearance, not exact mesh collision or physical acceptance'}))
    result=connection_checks(projected)
    # This pre-load check cannot grant acceptance for an unsupported topology.
    # Fresh loaded geometry is mandatory in the live workflow and routes missing capabilities to tools.
    if result['spatial_checks_applied']:
        require(result['passed'],'native layout geometry rejected: '+str(result['issues']))


def loaded_geometry(receipt,manifest):
    native=(receipt.get('native_geometry') or {}).get('objects')
    if not native: return {'passed':False,'scope':'geometry_not_available','spatial_checks_applied':False,'issues':[],
        'reason':'The loader supplied no native geometry; scene alignment has not been checked.'}
    ids={o['key']:o['id'] for o in manifest['objects']}
    result=connection_checks([{**o,'id':ids[o['key']]} for o in native if o['key'] in ids])
    for obj in native:
        if obj['key'] in ids and obj.get('native_registered') is False:
            result['issues'].append({'object_id':ids[obj['key']],
                'error':'native voxel registration failed: overlap or out-of-world placement',
                'bounds_world':obj['bounds_world'], 'evidence':'VoxelObject.RegistrationAllowed; EditContext.AddVoxelObject'})
    result['passed']=result['passed'] and not result['issues']
    return result


def attach_button_semantics(contract,manifest,receipt):
    ids={o['key']:o for o in manifest['objects']}
    for obj in (receipt.get('native_geometry') or {}).get('objects',[]):
        button=obj.get('component_geometry',{}).get('button')
        if not button or button.get('alternate_mode') is not False or obj['key'] not in ids: continue
        m=ids[obj['key']]
        for tag in m['tags']:
            if tag['kind']=='input' and tag['data_type']=='bool':
                contract['component_semantics'].append({'tag_key':tag['key'],'object_id':m['id'],'prefab':m['prefab'],
                    'semantics':{'function':'operator_button','pressed_value':not button['normally_closed'],
                                 'released_value':button['normally_closed']},
                    'app_version_confirmed':True,'verified_app_version':receipt['app_version'],
                    'evidence':[receipt['bridge_response_path']], 'bridge_response_sha256':receipt['bridge_response_sha256']})
