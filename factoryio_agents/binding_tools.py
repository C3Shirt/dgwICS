"""Derive BOOL channel assignments from a hash-verified, previously inspected template.

This prepares device mappings in a project copy. It generates no scene geometry or PLC logic.
"""
from copy import deepcopy
from pathlib import Path
import re

from .contracts import ContractError, file_hash, read_json, require
from .plc import run_codesys, transport_fingerprint


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
            m['kind'] == kind and m['channel_family'] == 'Bit' and descriptors[m['tag_key']]['bit_size'] == '1')
            for kind in ('input','output')}
        return {'channels':channels,'counts':{k:len(v) for k,v in channels.items()},
            'system_bool_inputs_reserved':1,'data_types':['bool'],
            'sources':{'seed_config_sha256':file_hash(source),'seed_io_sha256':file_hash(io_source),
                       'template_sha256':file_hash(template)},'scope':'existing inspected endpoints; no capacity expansion'}
    except (ContractError,OSError,KeyError,TypeError) as error:
        raise BindingConfigurationError('device capacity configuration: '+str(error)) from None


def bool_bindings(config: dict, contract: dict) -> dict:
    try:
        return _bool_bindings(config, contract)
    except (ContractError, OSError, KeyError, TypeError) as error:
        raise BindingConfigurationError('device binding configuration: ' + str(error)) from None


def _bool_bindings(config: dict, contract: dict) -> dict:
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
    bindings, verified = [], []
    for m in contract['mappings']:
        tag = tags[m['tag_key']]
        if tag.get('use_forced_value') is True: continue
        if m['channel_family'] != 'Bit' or tag['data_type'] != 'bool' or m['channel'] not in endpoints:
            raise BindingConfigurationError('automatic binding supports only inspected BOOL channels: ' + m['channel'])
        source = endpoints[m['channel']]
        require(source['bit_size'] == '1' and source['channel_type'] == m['direction'], 'seed channel direction/width differs')
        identity = re.sub('[^A-Za-z0-9_]', '_', tag['id'])[:48]
        variable = 'Application.SceneIO.' + ('I_' if tag['kind'] == 'input' else 'Q_') + identity + '_' + tag['key'][:8]
        bindings.append({'tag_key': tag['key'], 'variable': variable, 'plc_type': 'BOOL'})
        verified.append({**source, 'tag_key': tag['key'], 'variable': variable})
    require(bool(bindings), 'no programmable BOOL mappings')
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
