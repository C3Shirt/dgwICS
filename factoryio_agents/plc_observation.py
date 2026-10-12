"""Read scalar PLC state from the deployed project's existing online adapter."""
from pathlib import Path

from .contracts import canonical_hash, file_hash, read_json, require, write_json
from .plc import _declarations, run_codesys
from .runtime import utc_now


def observable_expressions(program):
    variables, _ = _declarations(program['declaration'], program['program_name'])
    scalar_types = {'BOOL','SINT','INT','DINT','LINT','USINT','UINT','UDINT','ULINT','BYTE','WORD','DWORD','LWORD','REAL','LREAL'}
    expressions = ['Application.' + program['program_name'] + '.' + name for name,type_ in variables.items()
                   if type_ in scalar_types]
    expressions += [b['variable'] for b in program.get('bindings', [])]
    return list(dict.fromkeys(expressions))[:50]


def observe_current_plc(config, plc_dir: Path, program, output: Path | None = None):
    """Copy the deployed project, log in without download, read values, log out.

    A snapshot can distinguish hidden latches; it is not a physical process pass.
    No expression writes, application starts/stops or forced values are used.
    """
    plc_dir = plc_dir.resolve()
    result = read_json(plc_dir / 'codesys-result.json')
    project = Path(result['project_path']).resolve()
    require(project.is_relative_to(plc_dir) and project.is_file(), 'deployed project is outside this PLC revision')
    require(file_hash(project) == result['project_sha256'], 'deployed project changed before PLC observation')
    require(canonical_hash(program) == read_json(plc_dir/'versions.json')['plc'], 'PLC observation source changed')
    output = output or plc_dir / 'state-observation'
    require(not output.exists(), 'PLC observation needs a fresh output directory')
    expressions = observable_expressions(program)
    require(bool(expressions), 'no supported scalar PLC expressions to observe')
    probe_config = {**config, 'template_project':str(project), 'deploy':False,
        'online_probe':{'connect':True, 'host':'127.0.0.1', 'port':11740,
                        'device_name':'Device', 'expressions':expressions}}
    response = run_codesys(probe_config, output, deploy=False)
    probe = response.get('online_probe') or {}
    receipt = {'schema_version':1, 'source':'codesys_online_read_value',
        'source_program_sha256':canonical_hash(program), 'source_project_sha256':file_hash(project),
        'observed_at':utc_now(), 'expressions':expressions, 'online_probe':probe,
        'verified':response.get('compiled') is True and response.get('source_unchanged') is True
            and probe.get('connected') is True and probe.get('application_state','').lower() == 'run'
            and not probe.get('monitor_error') and set(probe.get('values',{})) == set(expressions),
        'deploy_requested':response.get('deploy_requested'), 'physical_acceptance_passed':False,
        'response_path':str((output/'observation-result.json').resolve())}
    write_json(output/'observation-result.json', response)
    write_json(output/'observation-receipt.json', receipt)
    return receipt
