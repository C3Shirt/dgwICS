"""Ordered Factory I/O and PLC lifecycle operations, with observed receipts."""
from pathlib import Path
import time

from .contracts import canonical_hash, file_hash, read_json, require, write_json
from .plc import run_codesys
from .runtime import FactoryIOClient, utc_now


class SynchronizedRuntime:
    def __init__(self, factoryio, codesys, plc_dir: Path, output: Path):
        self.factoryio, self.codesys = factoryio, codesys
        self.runtime = FactoryIOClient(factoryio)
        self.plc_dir, self.output = plc_dir.resolve(), output.resolve()
        require(not self.output.exists(), 'lifecycle receipt directory already exists')
        deployed = read_json(self.plc_dir/'codesys-result.json')
        self.project = Path(deployed['project_path']).resolve()
        require(self.project.is_relative_to(self.plc_dir) and self.project.is_file(), 'PLC project is outside its revision')
        require(file_hash(self.project) == deployed['project_sha256'], 'deployed project changed')
        program = read_json(self.plc_dir/'program.json')
        self.protocol = read_json(self.plc_dir.parent/'io-contract.json')['protocol']
        require(canonical_hash(program) == read_json(self.plc_dir/'versions.json')['plc'], 'deployed PLC source changed')
        self.output.mkdir(parents=True)
        self.records = []
        self.require_current_scene()

    def record(self, action, result):
        self.records.append({'action':action, 'timestamp':utc_now(), 'result':result})
        write_json(self.output/'steps.json', self.records)
        return result

    def plc_action(self, action, mode=None):
        spec = {'connect':True, 'host':'127.0.0.1', 'port':11740, 'device_name':'Device', 'action':action}
        if mode is not None: spec['reset_mode'] = mode
        config = {**self.codesys, 'template_project':str(self.project), 'online_probe':spec, 'deploy':False}
        folder = self.output / ('plc-' + str(len(self.records)) + '-' + action)
        result = run_codesys(config, folder, deploy=False)
        write_json(folder/'operation-result.json', result)
        observed = result.get('online_probe') or {}
        self.record('plc_' + action, {'response_path':str((folder/'operation-result.json').resolve()),
            'response_sha256':file_hash(folder/'operation-result.json'), 'online':observed})
        require(result.get('compiled') is True and result.get('source_unchanged') is True
                and observed.get('status') == 'ok' and observed.get('connected') is True,
                'PLC lifecycle operation failed: ' + str(observed.get('error') or result.get('diagnostics')))
        expected = 'run' if action == 'start' else 'stop'
        require(observed.get('application_state','').lower() == expected, 'PLC lifecycle state not confirmed')
        require(observed.get(action + '_called') is True or (action in ('start','stop')
                and observed.get(action + '_already_satisfied') is True), 'PLC lifecycle method call not confirmed')
        if mode is not None: require(observed.get('reset_mode') == mode, 'PLC reset mode differs')
        return observed

    def freeze_factory(self):
        tags = self.runtime.tags()
        running = [t for t in tags if t['name'] == 'FACTORY I/O (Running)' and t['kind'].lower() == 'input']
        require(len(running) == 1 and running[0].get('isForced') is False, 'Running is unknown or forced')
        if running[0]['value']:
            self.runtime.control('pause', True)
            deadline = time.monotonic() + 5
            while True:
                paused = [t for t in self.runtime.tags() if t['name'] == 'FACTORY I/O (Paused)' and t['kind'].lower() == 'input']
                require(len(paused) == 1 and paused[0].get('isForced') is False, 'Paused is unknown or forced')
                if paused[0]['value'] is True: break
                require(time.monotonic() < deadline, 'Factory I/O pause was not observed')
                time.sleep(.1)
        self.record('factory_frozen', {'was_running':running[0]['value']})

    def stop_both(self):
        self.freeze_factory()
        self.plc_action('stop')
        self.runtime.control('run', False)
        self.record('factory_stopped', self.runtime.wait_running(False))
        self.runtime.control('pause', False)
        return self.finish('stop')

    def reset_both(self, mode='warm'):
        require(mode in ('warm','cold'), 'only warm/cold reset is supported')
        self.freeze_factory()
        self.plc_action('reset', mode)
        self.runtime.control('run', False)
        self.runtime.wait_running(False)
        self.runtime.control('reset', True)
        try: time.sleep(.25)
        finally: self.runtime.control('reset', False)
        self.runtime.control('pause', False)
        self.record('factory_reset_stopped', self.runtime.wait_running(False))
        self.require_current_scene(empty=True)
        return self.finish('reset', reset_mode=mode)

    def require_current_scene(self, empty=False):
        status = self.native_call('status')
        scene = self.plc_dir.parent/'scene.factoryio'
        data = status['data']
        require(Path(data['scene_path']).resolve() == scene.resolve() and data['scene_loading'] is False,
                'current Factory I/O scene differs from the PLC revision')
        if empty: require(data['item_count'] == 0, 'Factory I/O items were not cleared')
        self.record('native_scene_state', status)
        return data

    def native_call(self, action, **parameters):
        command = self.factoryio.get('load_command', [])
        require('--ipc' in command, 'synchronized lifecycle requires the native status bridge')
        import sys
        root = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(root/'tools/factoryio_bridge'))
        from client import call
        return call(Path(command[command.index('--ipc')+1]), action, **parameters)

    def reconnect(self):
        response = self.native_call('connect_modbus', **{k:self.protocol[k] for k in ('host','port','unit_id')})
        self.record('native_modbus_connect', response)
        if response['data'].get('ui',{}).get('window_opened') is True:
            self.record('acknowledge_modbus_notice', self.native_call('acknowledge_modbus_notice'))

    def start_both(self):
        self.runtime.wait_running(False)
        self.require_current_scene()
        self.plc_action('start')
        self.reconnect()
        deadline = time.monotonic() + 5
        while True:
            data = self.require_current_scene()
            modbus = data.get('modbus', {})
            if (modbus.get('connected') is True and modbus.get('socket_connected') is True
                    and modbus.get('has_error') is False and data.get('ui',{}).get('window_opened') is not True): break
            require(time.monotonic() < deadline, 'Modbus did not reconnect before scene start')
            time.sleep(.2)
        self.runtime.control('pause', False)
        self.runtime.control('run', True)
        self.record('factory_started', self.runtime.wait_running(True))
        return self.finish('start')

    def finish(self, action, **details):
        receipt = {'schema_version':1, 'action':action, 'completed_at':utc_now(),
                   'operation_passed':True, 'physical_acceptance_passed':False,
                   'ordered_operation':True, 'records':self.records, **details}
        write_json(self.output/'receipt.json', receipt)
        write_json(self.output/(action+'-receipt.json'), receipt)
        return receipt
