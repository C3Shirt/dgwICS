"""Derive channel assignments from a hash-verified, previously inspected template.

This prepares device mappings in a project copy. It generates no scene geometry or PLC logic.
"""
from copy import deepcopy
from pathlib import Path
import re

from .contracts import ContractError, file_hash, read_json, require
from .plc import run_codesys, transport_fingerprint, _PLC_BITS, _TYPES


class BindingConfigurationError(ContractError):
    """Device capacity/configuration does not support this scene; no scene repair."""


def inspected_io_capacity(config):
    """Expose existing verified endpoint limits to planning without expanding PLC memory."""
    try:
        source = Path(config['binding_seed_config']); io_source = Path(config['binding_seed_io'])
        seed = read_json(source)['codesys']; previous = read_json(io_source)
        template = Path(seed['template_project'])
        require(file_hash(template) == seed['verified_template_sha256'], 'binding seed template SHA changed')
        require(transport_fingerprint(previous) == seed['verified_transport_sha256'], 'binding seed transport SHA changed')
        descriptors = {r['tag_key']:r for r in seed['verified_device_bindings']}
        channels = {kind:sorted(m['channel'] for m in previous['mappings'] if m['tag_key'] in descriptors and
            m['kind'] == kind)
            for kind in ('input','output')}
        data_types=sorted({m['data_type'] for m in previous['mappings'] if m['tag_key'] in descriptors})
        return {'channels':channels,'counts':{k:len(v) for k,v in channels.items()},
            'system_bool_inputs_reserved':1,'data_types':data_types,
            'sources':{'seed_config_sha256':file_hash(source),'seed_io_sha256':file_hash(io_source),
                       'template_sha256':file_hash(template)},'scope':'existing inspected endpoints; no capacity expansion'}
    except (ContractError,OSError,KeyError,TypeError) as error:
        raise BindingConfigurationError('device capacity configuration: '+str(error)) from None


def bool_bindings(config: dict, contract: dict) -> dict:
    try:
        return _bool_bindings(config, contract)
    except (ContractError, OSError, KeyError, TypeError) as error:
        raise BindingConfigurationError('device binding configuration: ' + str(error)) from None


def verified_bindings(config: dict, contract: dict) -> dict:
    """Bind available verified endpoints; missing types/capacity are configuration gaps."""
    try:
        return _bool_bindings(config,contract,include_numeric=True)
    except (ContractError,OSError,KeyError,TypeError) as error:
        raise BindingConfigurationError('device binding configuration: '+str(error)) from None


def _bool_bindings(config: dict, contract: dict, *, include_numeric=False) -> dict:
    seed = read_json(Path(config['binding_seed_config']))
    previous = read_json(Path(config['binding_seed_io']))
    seed = seed['codesys']
    template = Path(seed['template_project'])
    require(file_hash(template) == seed['verified_template_sha256'], 'binding seed template SHA changed')
    require(previous.get('protocol') == contract.get('protocol'), 'binding seed uses another protocol')
    require(transport_fingerprint(previous) == seed['verified_transport_sha256'], 'binding seed transport SHA changed')
    require(previous['driver_properties'] == contract['driver_properties'] and
            previous['channel_offsets'] == contract['channel_offsets'], 'BOOL transport differs from inspected seed')
    descriptors = {r['tag_key']: r for r in seed['verified_device_bindings']}
    endpoints = {m['channel']: descriptors[m['tag_key']] for m in previous['mappings'] if m['tag_key'] in descriptors}
    tags = {t['key']: t for t in contract['tags']}
    old_tags={t['key']:t for t in previous['tags']}
    old_mappings={m['channel']:m for m in previous['mappings']}
    old_bindings={b['tag_key']:b for b in seed.get('bindings',[])}
    encodings={e['tag_key']:e for e in previous.get('wire_encodings',[])}
    bindings, verified = [], []
    for m in contract['mappings']:
        tag = tags[m['tag_key']]
        if tag.get('use_forced_value') is True: continue
        if not include_numeric and (m['channel_family'] != 'Bit' or tag['data_type'] != 'bool' or m['channel'] not in endpoints):
            raise BindingConfigurationError('automatic binding supports only inspected BOOL channels: ' + m['channel'])
        require(m['channel'] in endpoints,'no verified device endpoint for '+m['channel']+'; configure channel capacity/type before deployment')
        source = endpoints[m['channel']]
        old_key=old_mappings[m['channel']]['tag_key']
        require(old_tags[old_key]['data_type']==tag['data_type'],'verified endpoint data type differs: '+m['channel'])
        type_=old_bindings.get(old_key,{}).get('plc_type',source.get('plc_type'))
        if tag['data_type']=='bool':type_=type_ or 'BOOL'
        encoding=encodings.get(old_key)
        allowed={'WORD','UINT','INT'} if encoding else _TYPES.get(tag['data_type'],set())
        require(type_ in allowed,'missing or incompatible verified PLC type for '+m['channel'])
        require(int(source['bit_size'])==_PLC_BITS[type_] and source['channel_type']==m['direction'],'seed channel direction/width differs')
        if tag['data_type']!='bool' and contract.get('driver')=='ModbusTCPClient':
            require(encoding is not None,'numeric Modbus endpoint lacks verified wire encoding: '+m['channel'])
            current_encoding={**encoding,'tag_key':tag['key']}
            existing=next((e for e in contract.get('wire_encodings',[]) if e['tag_key']==tag['key']),None)
            require(existing is None or existing==current_encoding,'numeric wire encoding differs from verified endpoint: '+m['channel'])
            if existing is None:contract.setdefault('wire_encodings',[]).append(current_encoding)
        identity = re.sub('[^A-Za-z0-9_]', '_', tag['id'])[:48]
        suffix=re.sub('[^A-Za-z0-9_]','_',tag['key'][:8])
        variable = config.get('application_name','Application')+'.SceneIO.' + ('I_' if tag['kind'] == 'input' else 'Q_') + identity + '_' + suffix
        bindings.append({'tag_key': tag['key'], 'variable': variable, 'plc_type': type_})
        verified.append({**source, 'tag_key': tag['key'], 'variable': variable})
    require(bool(bindings), 'no programmable mappings')
    result = deepcopy(config)
    result.update(template_project=str(template), bindings=bindings, verified_device_bindings=verified,
                  verified_template_sha256=seed['verified_template_sha256'], transport_confirmed=True,
                  verified_transport_sha256=transport_fingerprint(contract))
    result['device_patch'] = {'source_sha256': file_hash(template), 'device_path': verified[0]['device_path'],
        'connector_id': verified[0]['connector_id'], 'parameters': [], 'bindings': verified, 'clear_existing_mappings': True}
    return result


def compile_bound_program(config: dict, output: Path, program: dict, contract: dict, deploy: bool) -> dict:
    if not config.get('auto_bool_bindings'):
        return run_codesys(config, output, program=program, io_contract=contract, deploy=deploy)
    prepared = run_codesys(config, output / 'template-preparation', program=program, io_contract=contract, deploy=False)
    if not prepared.get('compiled') or not prepared.get('bindings_verified') or not prepared.get('task_program_verified'):
        return prepared
    if not deploy: return prepared
    effective = deepcopy(config)
    effective.update(device_patch=None, template_project=prepared['project_path'],
                     verified_template_sha256=prepared['project_sha256'])
    result = run_codesys(effective, output / 'deployment', program=program, io_contract=contract, deploy=True)
    result['template_preparation'] = {k: prepared.get(k) for k in
        ('compiled', 'bindings_verified', 'task_program_verified', 'project_path', 'project_sha256', 'source_unchanged')}
    return result
