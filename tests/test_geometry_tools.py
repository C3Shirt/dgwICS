from copy import deepcopy
import unittest

from factoryio_agents.contracts import ContractError
from factoryio_agents.geometry_tools import project_object, ray_crosses_footprint, validate_layout_geometry, loaded_geometry, attach_button_semantics, resolve_relative_layout


class GeometryToolTests(unittest.TestCase):
    def test_registration_check_does_not_require_transport_or_a_conveyor_target(self):
        native=[{'key':'tank','prefab':'Tank','native_registered':True,'component_geometry':{}},
                {'key':'eye','prefab':'DiffusePhotoelectricSensor','native_registered':True,
                 'component_geometry':{'sensor':{'origin':[0,1,0],'direction':[1,0,0],'range_metres':1}}}]
        receipt={'native_geometry':{'objects':native}}
        manifest={'objects':[{'key':'tank','id':'vessel'},{'key':'eye','id':'position_detector'}]}
        result=loaded_geometry(receipt,manifest,check_connections=False)
        self.assertTrue(result['passed']);self.assertTrue(result['registration_checks_applied'])
        self.assertFalse(result['spatial_checks_applied'])
        native[0]['native_registered']=False
        self.assertFalse(loaded_geometry(receipt,manifest,check_connections=False)['passed'])

    def setUp(self):
        self.native={'coordinate_system':{'axis_order':['X','Y','Z'],'vertical_axis':'Y','grid_metres':0.125},'prefabs':{
            'RollerConveyor6M':{'bounds_local_metres':{'min':[-.5,-.3,-3],'max':[.5,.3,3]},
                'component_geometry_local':{'conveyor':{'direction':[0,0,1]}}},
            'Emitter':{'bounds_local_metres':{'min':[-.4,-.2,-.5],'max':[.4,.3,.5]},
                'component_geometry_local':{'emitter':{'origin':[0,-.125,0]}}},
            'Sensor':{'bounds_local_metres':{'min':[-.0625]*3,'max':[.0625]*3},
                'component_geometry_local':{'sensor':{'origin':[0,0,0],'direction':[-1,0,0],'range_metres':.8}}}}}
        self.catalog={'native_geometry':self.native,'templates':[{'template_id':k,'prefab':k} for k in self.native['prefabs']]}
        self.plan={'objects':[{'id':'belt','template_id':'RollerConveyor6M','position':[100,2,100],'yaw_deg':0},
            {'id':'feed','template_id':'Emitter','position':[100,6,80],'yaw_deg':0},
            {'id':'eye','template_id':'Sensor','position':[106,5,100],'yaw_deg':0}]}

    def test_positive_yaw_rotates_native_long_axis_from_z_to_x(self):
        obj=self.plan['objects'][0];obj['yaw_deg']=90
        value=project_object(obj,'RollerConveyor6M',self.native['prefabs']['RollerConveyor6M'],.125)
        self.assertEqual(value['component_geometry']['conveyor']['direction'],[1,0,0])
        b=value['bounds_world'];self.assertAlmostEqual(b['max'][0]-b['min'][0],6)
        self.assertAlmostEqual(b['max'][2]-b['min'][2],1)
        self.assertAlmostEqual((b['min'][1]+b['max'][1])/2,(2+.5)*.125)

    def test_feasible_connections_pass_without_modifying_model_layout(self):
        before=deepcopy(self.plan);validate_layout_geometry(self.plan,self.catalog)
        self.assertEqual(self.plan,before)

    def test_template_source_offset_outside_belt_is_rejected(self):
        self.plan['objects'][1]['position'][0]+=20
        with self.assertRaisesRegex(ContractError,'emission origin is outside'):validate_layout_geometry(self.plan,self.catalog)

    def test_sensor_facing_away_or_too_far_is_rejected(self):
        self.plan['objects'][2]['yaw_deg']=180
        with self.assertRaisesRegex(ContractError,'sensor ray cannot reach'):validate_layout_geometry(self.plan,self.catalog)

    def test_native_loaded_ray_has_priority_over_prefab_projection(self):
        belt=project_object(self.plan['objects'][0],'RollerConveyor6M',self.native['prefabs']['RollerConveyor6M'],.125)
        feed=project_object(self.plan['objects'][1],'Emitter',self.native['prefabs']['Emitter'],.125)
        eye=project_object(self.plan['objects'][2],'Sensor',self.native['prefabs']['Sensor'],.125)
        eye['component_geometry']['sensor']['range_metres']=.05
        rows=[{**o,'key':o['id']} for o in (belt,feed,eye)]
        result=loaded_geometry({'native_geometry':{'objects':rows}},{'objects':[{'key':o['id'],'id':o['id']} for o in rows]})
        self.assertFalse(result['passed']);self.assertEqual(result['issues'][0]['object_id'],'eye')

    def test_normally_closed_button_is_recorded_from_native_receipt(self):
        contract={'component_semantics':[]};manifest={'objects':[{'key':'stop','id':'operator_stop','prefab':'StopPushButton',
            'tags':[{'key':'input','kind':'input','data_type':'bool'}]}]}
        receipt={'native_geometry':{'objects':[{'key':'stop','component_geometry':{'button':{'normally_closed':True,'alternate_mode':False}}}]},
            'app_version':'2.5.8','bridge_response_path':'receipt.json','bridge_response_sha256':'hash'}
        attach_button_semantics(contract,manifest,receipt)
        self.assertEqual(contract['component_semantics'][0]['semantics'],{'function':'operator_button','pressed_value':False,'released_value':True})

    def test_relative_constraints_resolve_both_sides_for_each_conveying_direction(self):
        for yaw in (0,90,180,270):
            for side in ('left','right'):
                with self.subTest(yaw=yaw,side=side):
                    constraints={'objects':[{'id':'belt','template_id':'RollerConveyor6M','placement':{'kind':'anchor','position':[100,2,100],'yaw_deg':yaw}},
                        {'id':'feed','template_id':'Emitter','placement':{'kind':'emitter_over_conveyor','relative_to':'belt','along_fraction':.1}},
                        {'id':'eye','template_id':'Sensor','placement':{'kind':'sensor_beside_conveyor','relative_to':'belt','along_fraction':.8,
                            'side':side,'height_above_surface_grid':1,'clearance_grid':1}}]}
                    original=deepcopy(constraints);resolved=resolve_relative_layout(constraints,self.catalog)
                    validate_layout_geometry(resolved,self.catalog)
                    self.assertEqual(constraints,original)
                    self.assertTrue(all(all(type(x) is int for x in o['position']) for o in resolved['objects']))
                    feed=next(o for o in resolved['objects'] if o['id']=='feed')
                    self.assertGreater(feed['position'][1],2)

    def test_offsets_cannot_bypass_measured_sensor_placement(self):
        constraints={'objects':[{'id':'belt','template_id':'RollerConveyor6M','placement':{'kind':'anchor','position':[100,2,100],'yaw_deg':0}},
            {'id':'eye','template_id':'Sensor','placement':{'kind':'grid_offset','relative_to':'belt','offset_grid':[0,3,0],'yaw_deg':0}}]}
        with self.assertRaisesRegex(ContractError,'not feed or detection'):resolve_relative_layout(constraints,self.catalog)

    def test_multiple_conveyors_resolve_each_sensor_against_its_named_reference(self):
        constraints={'objects':[
            {'id':'a','template_id':'RollerConveyor6M','placement':{'kind':'anchor','position':[100,2,100],'yaw_deg':0}},
            {'id':'b','template_id':'RollerConveyor6M','placement':{'kind':'absolute','position':[200,2,200],'yaw_deg':90}},
            {'id':'eye','template_id':'Sensor','placement':{'kind':'sensor_beside_conveyor','relative_to':'b',
                'along_fraction':.8,'side':'left','height_above_surface_grid':1,'clearance_grid':1}}]}
        try:plan=resolve_relative_layout(constraints,self.catalog,verify=False)
        except ContractError as error:self.fail('Multiple conveyor references must be supported: '+str(error))
        a,b,eye=[project_object(o,o['template_id'],self.native['prefabs'][o['template_id']],.125) for o in plan['objects']]
        ray=eye['component_geometry']['sensor']
        self.assertTrue(ray_crosses_footprint(ray['origin'],ray['direction'],ray['range_metres'],b['bounds_world']))
        self.assertFalse(ray_crosses_footprint(ray['origin'],ray['direction'],ray['range_metres'],a['bounds_world']))

    def test_conveyor_chains_use_measured_ports_in_all_cardinal_directions(self):
        for yaw in (0,90,180,270):
            constraints={'objects':[
                {'id':'c','template_id':'RollerConveyor6M','placement':{'kind':'conveyor_after_conveyor','relative_to':'b','clearance_grid':0}},
                {'id':'b','template_id':'RollerConveyor6M','placement':{'kind':'conveyor_after_conveyor','relative_to':'a','clearance_grid':0}},
                {'id':'a','template_id':'RollerConveyor6M','placement':{'kind':'anchor','position':[100,2,100],'yaw_deg':yaw}}]}
            try:plan=resolve_relative_layout(constraints,self.catalog,verify=False)
            except ContractError as error:self.fail('Measured conveyor chaining must be supported: '+str(error))
            projected={o['id']:project_object(o,o['template_id'],self.native['prefabs'][o['template_id']],.125) for o in plan['objects']}
            axis=0 if yaw%180 else 2
            for parent,child in (('a','b'),('b','c')):
                pa=projected[parent];ch=projected[child];positive=pa['component_geometry']['conveyor']['direction'][axis]>0
                self.assertAlmostEqual(pa['bounds_world']['max' if positive else 'min'][axis],ch['bounds_world']['min' if positive else 'max'][axis])
                self.assertAlmostEqual(pa['bounds_world']['max'][1],ch['bounds_world']['max'][1])

    def test_cyclic_conveyor_references_are_rejected(self):
        value={'objects':[{'id':i,'template_id':'RollerConveyor6M','placement':{
            'kind':'conveyor_after_conveyor','relative_to':other,'clearance_grid':0}} for i,other in (('a','b'),('b','a'))]}
        with self.assertRaisesRegex(ContractError,'cycle'):resolve_relative_layout(value,self.catalog,verify=False)

    def test_loaded_multi_conveyor_connections_are_checked_without_single_belt_gate(self):
        objects=deepcopy(self.plan)
        second=deepcopy(objects['objects'][0]);second.update(id='second',position=[100,2,148])
        objects['objects'].append(second);objects['objects'][2]['position']=[106,5,148]
        rows=[{**project_object(o,o['template_id'],self.native['prefabs'][o['template_id']],.125),'key':o['id']} for o in objects['objects']]
        result=loaded_geometry({'native_geometry':{'objects':rows}},{'objects':[{'key':o['id'],'id':o['id']} for o in rows]})
        self.assertTrue(result['spatial_checks_applied']);self.assertTrue(result['passed'])
        rows[-1]['bounds_world']['min'][2]+=.5;rows[-1]['bounds_world']['max'][2]+=.5
        result=loaded_geometry({'native_geometry':{'objects':rows}},{'objects':[{'key':o['id'],'id':o['id']} for o in rows]})
        self.assertFalse(result['passed'])
        self.assertTrue(any(i['object_id']=='second' for i in result['issues']))

    def test_remover_volume_begins_after_conveyor_for_all_cardinal_flows(self):
        self.native['prefabs']['Remover']={'bounds_local_metres':{'min':[-.4,-.2,-.5],'max':[.4,.3,.5]},'component_geometry_local':{}}
        self.catalog['templates'].append({'template_id':'Remover','prefab':'Remover'})
        for yaw in (0,90,180,270):
            constraints={'objects':[{'id':'belt','template_id':'RollerConveyor6M','placement':{'kind':'anchor','position':[100,2,100],'yaw_deg':yaw}},
                {'id':'remove','template_id':'Remover','placement':{'kind':'remover_after_conveyor','relative_to':'belt','clearance_grid':0}}]}
            resolved=resolve_relative_layout(constraints,self.catalog)
            belt,remover=[project_object(o,o['template_id'],self.native['prefabs'][o['template_id']],.125) for o in resolved['objects']]
            direction=belt['component_geometry']['conveyor']['direction'];axis=0 if yaw%180 else 2
            if direction[axis]>0:self.assertGreaterEqual(remover['bounds_world']['min'][axis],belt['bounds_world']['max'][axis])
            else:self.assertLessEqual(remover['bounds_world']['max'][axis],belt['bounds_world']['min'][axis])
            self.assertGreaterEqual(remover['bounds_world']['min'][1],belt['bounds_world']['max'][1])

    def test_native_registration_failure_is_not_a_visual_pass(self):
        receipt={'native_geometry':{'objects':[{'key':'r','prefab':'Remover','bounds_world':{'min':[0]*3,'max':[1]*3},
            'component_geometry':{},'native_registered':False}]}}
        result=loaded_geometry(receipt,{'objects':[{'key':'r','id':'remover'}]})
        self.assertFalse(result['passed']);self.assertIn('native voxel registration failed',result['issues'][0]['error'])

    def test_missing_native_geometry_is_not_a_success(self):
        result = loaded_geometry({}, {'objects':[]})
        self.assertFalse(result['passed'])
        self.assertFalse(result['spatial_checks_applied'])
        self.assertEqual(result['scope'],'geometry_not_available')

    def test_unmeasured_belt_cannot_silently_skip_connection_checks(self):
        belt = project_object(self.plan['objects'][0],'RollerConveyor6M',self.native['prefabs']['RollerConveyor6M'],.125)
        belt.update(prefab='BeltConveyor4M',component_geometry={})
        feed = project_object(self.plan['objects'][1],'Emitter',self.native['prefabs']['Emitter'],.125)
        rows = [{**o,'key':o['id']} for o in (belt,feed)]
        result = loaded_geometry({'native_geometry':{'objects':rows}},
            {'objects':[{'key':o['id'],'id':o['id']} for o in rows]})
        self.assertFalse(result['passed'])
        self.assertFalse(result['spatial_checks_applied'])
        self.assertEqual(result['issues'][0]['target'],'tools')
        self.assertEqual(result['issues'][0]['object_id'],'belt')

    def test_unsupported_topology_does_not_become_pass_when_registration_is_healthy(self):
        rows = [{'key':'one','prefab':'OtherMachine','native_registered':True,'component_geometry':{},
                 'bounds_world':{'min':[0]*3,'max':[1]*3}}]
        result = loaded_geometry({'native_geometry':{'objects':rows}},{'objects':[{'key':'one','id':'machine'}]})
        self.assertFalse(result['passed'])
        self.assertFalse(result['spatial_checks_applied'])
        self.assertEqual(result['scope'],'unsupported_topology')

    def test_scene_is_centered_as_one_assembly_and_keeps_relative_positions(self):
        from factoryio_agents.geometry_tools import center_scene_layout
        self.native['world_bounds']={'min':[0,0,0],'max':[59.625,9,35.625],
            'evidence':'VoxelWorld.Dimensions; VoxelWorld.Encompasses'}
        before=deepcopy(self.plan)
        result,receipt=center_scene_layout(self.plan,self.catalog)
        self.assertEqual(self.plan,before)
        for a,b in zip(before['objects'],result['objects']):
            self.assertEqual([y-x for x,y in zip(a['position'],b['position'])],receipt['translation_grid'])
            self.assertEqual(a['yaw_deg'],b['yaw_deg'])
        for axis in (0,2):
            self.assertLessEqual(abs(receipt['result_center_world'][axis]-receipt['target_center_world'][axis]),.0625+1e-8)
        self.assertGreaterEqual(receipt['result_bounds_world']['min'][1],0)

    def test_centering_lifts_negative_floor_bounds_and_rejects_an_oversized_scene(self):
        from factoryio_agents.geometry_tools import center_scene_layout
        self.native['world_bounds']={'min':[0,0,0],'max':[59.625,9,35.625]}
        self.plan['objects'][0]['position']=[0,0,0]
        result,receipt=center_scene_layout(self.plan,self.catalog)
        self.assertGreater(receipt['translation_grid'][1],0)
        self.assertGreaterEqual(receipt['result_bounds_world']['min'][1],0)
        self.plan['objects'][1]['position'][0]=1000
        with self.assertRaisesRegex(ContractError,'does not fit'):
            center_scene_layout(self.plan,self.catalog)

    def test_centering_never_invents_unmeasured_factory_dimensions(self):
        from factoryio_agents.geometry_tools import center_scene_layout
        result,receipt=center_scene_layout(self.plan,self.catalog)
        self.assertEqual(result,self.plan)
        self.assertIsNone(receipt)


if __name__=='__main__':unittest.main()
