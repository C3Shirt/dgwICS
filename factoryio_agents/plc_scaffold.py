"""Tool-owned I/O declarations; model-owned local state and control logic.

This module does not command actuators or create a state machine. It composes
the exact model fragment with declarations derived from verified device bindings.
Optional normalized copies of measured inputs use only evidence-confirmed polarity.
"""
from copy import deepcopy
import re

from .contracts import canonical_hash, require
from .plc import _name, _declarations, _mask, validate_program, _TYPES, _PLC_BITS


def io_scaffold(config, contract):
    require(type(config.get('normalize_bool_inputs',False)) is bool,'normalize_bool_inputs must be boolean')
    bindings = deepcopy(config.get('bindings', []))
    require(bindings and bindings == contract.get('bindings'), 'I/O scaffold requires matching verified bindings')
    verified = {b['tag_key']: b for b in config.get('verified_device_bindings', [])}
    tags = {t['key']: t for t in contract['tags']}
    scopes, signals, keys, variables, normalization = {}, [], set(), set(), []
    for binding in bindings:
        require(set(binding) == {'tag_key','variable','plc_type'}, 'invalid scaffold binding fields')
        key, variable, type_ = (binding[k] for k in ('tag_key','variable','plc_type'))
        require(key in tags and key not in keys and variable.casefold() not in variables, 'duplicate/unknown scaffold binding')
        encoding=next((e for e in contract.get('wire_encodings',[]) if e['tag_key']==key),None)
        allowed={'WORD','UINT','INT'} if encoding else _TYPES.get(tags[key]['data_type'],set())
        require(type_ in allowed,'I/O scaffold PLC type is incompatible with the tag')
        require(verified.get(key, {}).get('variable') == variable and int(verified[key].get('bit_size',0)) == _PLC_BITS[type_],
                'scaffold variable is not a verified device binding')
        parts = variable.split('.')
        require(len(parts) == 3 and parts[0] == config.get('application_name', 'Application'), 'scaffold needs application-qualified bindings')
        scope, name = (_name(p, 'scaffold identifier') for p in parts[1:])
        require(scope.casefold() != config.get('program_name', 'PLC_PRG').casefold(), 'I/O scope collides with program')
        scopes.setdefault(scope, []).append((name, type_))
        signal={**binding, 'st_reference':scope+'.'+name,
                **{k:tags[key].get(k) for k in ('object_id','id','name','kind','data_type','system_field')}}
        if encoding:signal['wire_encoding']=deepcopy(encoding)
        if config.get('normalize_bool_inputs') and tags[key]['kind']=='input' and tags[key]['data_type']=='bool':
            rows=[r for r in contract.get('component_semantics',[]) if r['tag_key']==key and r.get('app_version_confirmed') is True and r.get('evidence')]
            if tags[key].get('system_field')=='RunInputKey':
                active=True;meaning='simulation_running'
            else:
                sem=rows[0]['semantics'] if len(rows)==1 else {}
                field='pressed_value' if sem.get('function')=='operator_button' else 'occupied_value'
                if type(sem.get(field)) is not bool:
                    signal['polarity']='unconfirmed_raw_value'
                    signals.append(signal);keys.add(key);variables.add(variable.casefold())
                    continue
                active=sem[field];meaning='pressed' if field=='pressed_value' else 'occupied'
            alias='SceneInputs.'+name
            scopes.setdefault('SceneInputs',[]).append((name,'BOOL'))
            normalization.append({'tag_key':key,'source':signal['st_reference'],'target':alias,'active_value':active,
                                  'meaning':meaning,'evidence':deepcopy(rows) if rows else ['native RunInputKey / Running mapping']})
            signal.update(st_reference=alias,raw_st_reference=scope+'.'+name,normalized_meaning=meaning,
                          normalized_active_value=True)
        signals.append(signal)
        keys.add(key); variables.add(variable.casefold())
    globals_ = [{'name':scope, 'declaration':'VAR_GLOBAL\n'+''.join(
        '    '+name+' : '+type_+';\n' for name,type_ in sorted(rows))+'END_VAR'}
        for scope,rows in sorted(scopes.items())]
    result={'schema_version':1, 'program_name':config.get('program_name','PLC_PRG'),
            'globals':globals_, 'bindings':bindings, 'signals':signals,
            'authority':'verified_device_bindings', 'io_sha256':canonical_hash(contract)}
    if config.get('normalize_bool_inputs'):result['normalization']=normalization
    return result


def assemble_program(fragment, scaffold, contract):
    expected = {'schema_version','handoff_id','program_name','declaration','implementation','unresolved','notes'}
    require(isinstance(fragment,dict) and set(fragment) == expected,
            'logic fragment must contain only schema_version, handoff_id, program_name, declaration, implementation, unresolved, notes; tools own globals and bindings')
    require(scaffold['io_sha256'] == canonical_hash(contract), 'I/O scaffold belongs to another contract')
    require(fragment['program_name'] == scaffold['program_name'], 'logic fragment changed the configured program name')
    prologue='\n'.join(r['target']+' := '+('' if r['active_value'] else 'NOT ')+r['source']+';' for r in scaffold.get('normalization',[]))
    result = {**deepcopy(fragment), 'globals':deepcopy(scaffold['globals']), 'bindings':deepcopy(scaffold['bindings'])}
    if prologue:result['implementation']='(* Tool: evidence-confirmed input normalization *)\n'+prologue+'\n\n'+fragment['implementation']
    validate_program(result, contract)
    locals_, _ = _declarations(result['declaration'], result['program_name'])
    short_names = {b['variable'].split('.')[-1].casefold() for b in scaffold['bindings']}
    require(not short_names.intersection(locals_), 'logic fragment shadows tool-owned I/O variables')
    code = _mask(fragment['implementation'])
    for signal in scaffold['signals']:
        # Physical inputs remain read-only. The IDE checks ST syntax; unused
        # optional channels and function-block output connections are permitted.
        ref = signal['st_reference']
        pattern = r'(?<![A-Za-z0-9_])'+re.escape(ref)+r'(?![A-Za-z0-9_])'
        if signal['kind'] == 'input':
            require(re.search(pattern+r'\s*:=',code,re.I) is None and re.search(r'=>\s*'+pattern,code,re.I) is None,
                    'logic writes a physical input: '+ref)
            if signal.get('raw_st_reference'):
                require(re.search(r'(?<![A-Za-z0-9_])'+re.escape(signal['raw_st_reference'])+r'(?![A-Za-z0-9_])',code,re.I) is None,
                        'logic must use normalized input rather than reinterpret raw polarity: '+ref)
    return result
