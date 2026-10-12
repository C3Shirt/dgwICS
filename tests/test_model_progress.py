"""Public display/stream regressions use local fixtures, never simulation or training gold."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from factoryio_agents.contracts import read_json, write_json
from factoryio_agents.llm import ModelClient, ModelServiceError, ModelOutputError
from factoryio_agents.progress import ConsoleProgress, ModelObserver, ObservedClient, PublicJSONStream, response_summary, replay_models, safe_text
from factoryio_agents.training import RecordingClient, TrainingRecorder


class StreamResponse(io.BytesIO):
    status=200
    def __init__(self, events):
        super().__init__((''.join('data: '+(event if isinstance(event,str) else json.dumps(event,ensure_ascii=False))+'\n\n'
                                for event in events)).encode('utf-8'))
        self.headers={'Content-Type':'text/event-stream; charset=utf-8'}


def packets(value, finish='stop', done=True):
    text=json.dumps(value,ensure_ascii=False)
    events=[{'model':'fixture-model','id':'fixture-stream','choices':[{'index':0,'delta':{'reasoning_content':'PRIVATE REASONING FIXTURE'}}]}]
    events += [{'choices':[{'index':0,'delta':{'content':text[i:i+7]}}]} for i in range(0,len(text),7)]
    if finish is not None: events.append({'choices':[{'index':0,'delta':{},'finish_reason':finish}]})
    events.append({'choices':[],'usage':{'prompt_tokens':7,'completion_tokens':11,'total_tokens':18}})
    if done: events.append('[DONE]')
    return events


class ModelProgressTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.output=Path(self.tmp.name)

    def client(self):
        return ModelClient({'base_url':'https://fixture.invalid/v1','models':{'plc':'fixture-model'}})

    def test_inline_assignment_filter_keeps_following_explanation(self):
        text=safe_text('Correct `state := 0` or initial startup; keep conveyor running.')
        self.assertNotIn('state := 0',text)
        self.assertIn('or initial startup; keep conveyor running.',text)

    def test_incremental_lexer_handles_fragmented_escapes_and_blocks_code(self):
        fields=[]; lexer=PublicJSONStream(lambda path,text:fields.append((path,text)))
        value={'objects':[{'id':'one','purpose':'输送 "箱子"\n下一件'}],
            'implementation':'SECRET PROGRAM CODE','globals':[{'declaration':'SECRET GVL'}],
            'notes':['保持一次一件'], 'xml':'<secret>full XML</secret>','nested':{'password':'PRIVATE PASSWORD'}}
        text=json.dumps(value,ensure_ascii=False)
        for char in text: lexer.feed(char)
        self.assertEqual(fields,[(('objects',0,'purpose'),'输送 "箱子"\n下一件'),(('notes',0),'保持一次一件')])

    def test_sse_returns_identical_json_usage_and_public_text_without_private_reasoning(self):
        client=self.client(); events=[]; client.set_progress_callback(events.append)
        value={'program_name':'PLC_PRG','implementation':'CODE-ONLY-FIXTURE','notes':['出口清空再供料']}
        with patch.object(client.opener,'open',return_value=StreamResponse(packets(value))) as opened:
            self.assertEqual(client.complete('plc','JSON',{}),value)
        body=json.loads(opened.call_args.args[0].data)
        self.assertTrue(body['stream']); self.assertEqual(body['stream_options'],{'include_usage':True})
        exchange=client.exchange_metadata()
        self.assertEqual(exchange['usage']['total_tokens'],18)
        self.assertTrue(exchange['stream_stats']['done_received'])
        self.assertEqual(exchange['response_mode'],'sse')
        self.assertNotIn('PRIVATE REASONING',json.dumps(exchange))
        public=[e for e in events if e['event']=='public_field']
        self.assertEqual([e['text'] for e in public],['出口清空再供料'])
        self.assertNotIn('CODE-ONLY-FIXTURE',json.dumps(events))

    def test_interrupted_stream_keeps_partial_completion_but_never_returns_success(self):
        client=self.client(); client.set_progress_callback(lambda e:None)
        with patch.object(client.opener,'open',return_value=StreamResponse(packets({'notes':['部分输出']},finish=None,done=False))):
            with self.assertRaises(ModelServiceError): client.complete('plc','JSON',{})
        self.assertIn('部分输出',client.exchange_metadata()['raw_content'])
        self.assertFalse(client.exchange_metadata()['stream_stats']['done_received'])

    def test_stream_length_finish_remains_output_budget_error(self):
        client=self.client(); client.set_progress_callback(lambda e:None)
        with patch.object(client.opener,'open',return_value=StreamResponse(packets({},finish='length'))):
            with self.assertRaises(ModelOutputError): client.complete('plc','JSON',{})

    def test_json_reply_to_stream_request_is_used_once_and_labeled(self):
        client=self.client(); client.set_progress_callback(lambda e:None)
        response=io.BytesIO(b'{"choices":[{"message":{"content":"{}"},"finish_reason":"stop"}]}')
        with patch.object(client.opener,'open',return_value=response) as opened:
            self.assertEqual(client.complete('plc','JSON',{}),{})
        opened.assert_called_once()
        self.assertEqual(client.exchange_metadata()['response_mode'],'json')

    def test_heartbeat_does_not_invent_model_text_and_stops_at_completion(self):
        class SlowFixture:
            def complete(self,*args):time.sleep(.075);return {'notes':['完成一次等待测试']}
        events=[]; observer=ModelObserver(self.output,events.append,heartbeat_seconds=.015)
        output=ObservedClient(SlowFixture(),observer).complete('plc','JSON',{})
        self.assertEqual(output,{'notes':['完成一次等待测试']})
        waits=[e for e in events if e['event']=='model_waiting']
        self.assertGreaterEqual(len(waits),1)
        self.assertTrue(all(e['content_chars']==0 for e in waits))
        before=len(events);time.sleep(.03);self.assertEqual(before,len(events))

    def test_recording_wrapper_preserves_candidate_origin_and_never_labels_gold(self):
        client=self.client(); console=ConsoleProgress(self.output,quiet=True)
        observer=ModelObserver(self.output,console,secrets=['fixture-secret'])
        recorder=TrainingRecorder(self.output/'training-data',secrets=['fixture-secret'],event_callback=observer.training_event)
        wrapped=RecordingClient(ObservedClient(client,observer,recorder),recorder)
        value={'implementation':'CODE-BODY-DO-NOT-PRINT','notes':['遵循 fixture-secret 对应的配置']}
        with patch.object(client.opener,'open',return_value=StreamResponse(packets(value))):
            self.assertEqual(wrapped.complete('plc','JSON',{}),value)
        record=read_json(next((recorder.directory/'candidates').glob('*.json')))
        self.assertEqual(record['output_origin'],'provider_content')
        self.assertEqual(record['model_metadata']['usage']['total_tokens'],18)
        self.assertEqual(record['model_metadata']['response_mode'],'sse')
        self.assertFalse(record['quality']['sft_eligible'])
        for path in [self.output/'progress.log',self.output/'model-events.jsonl']:
            text=path.read_text(encoding='utf-8')
            self.assertNotIn('fixture-secret',text); self.assertNotIn('CODE-BODY-DO-NOT-PRINT',text)
            self.assertNotIn('PRIVATE REASONING',text)
            if path.name=='progress.log': self.assertIn('尚待',text)

    def test_contract_rejection_shows_error_without_dumping_rejected_artifact(self):
        console=ConsoleProgress(self.output,quiet=True); observer=ModelObserver(self.output,console)
        observer.last_context={'active_role':'scene','phase':'layout'}
        observer.training_event({'event':'scene_substage_rejected','candidate_id':'fixture','details':{
            'stage':'layout','error':'sensor below conveyor','previous_response':{'xml':'DO-NOT-PRINT'}}})
        text=(self.output/'progress.log').read_text(encoding='utf-8')
        self.assertIn('sensor below conveyor',text);self.assertNotIn('DO-NOT-PRINT',text)

    def test_summary_displays_rules_and_diagnosis_without_serializing_program(self):
        value={'program_name':'PLC_PRG','implementation':'HIDDEN ST','declaration':'HIDDEN VAR',
            'notes':['保持单件循环'], 'findings':[{'target':'scene','observed':'入口未检测','expected':'箱子经过入口',
            'suggestion':'检查检测高度','diagnosis':{'causal_status':'hypothesis','discriminating_check':'测量箱体与射线高度'}}]}
        text='\n'.join(response_summary('plc',value,{}))
        self.assertIn('检查检测高度',text);self.assertIn('hypothesis',text);self.assertIn('保持单件循环',text)
        self.assertNotIn('HIDDEN ST',text);self.assertNotIn('HIDDEN VAR',text)

    def test_replay_excludes_copied_lineage_and_does_not_write_run(self):
        write_json(self.output/'run.json',{'created_at':'2026-10-11T10:00:00+00:00'})
        for index,stamp in enumerate(['2026-10-10T10:00:00+00:00','2026-10-11T10:01:00+00:00']):
            write_json(self.output/f'training-data/candidates/{index}.json',{'candidate_id':str(index),'role':'planner',
                'started_at':stamp,'payload':{'planning_stage':'spec'},'structured_output':{'control_rules':['供料一次一件']}})
        before={str(p):p.read_bytes() for p in self.output.rglob('*') if p.is_file()}
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            count=replay_models(self.output,ConsoleProgress(None))
        self.assertEqual(count,1);self.assertIn('供料一次一件',stdout.getvalue())
        self.assertEqual(before,{str(p):p.read_bytes() for p in self.output.rglob('*') if p.is_file()})

    def test_probe_summary_displays_the_actual_invalid_release_window(self):
        value = {'probes':[{'id':'startup','actions':[
            {'label':'press','object_id':'button','pressed':True,'hold_seconds':.8},
            {'label':'release','object_id':'button','pressed':False,'hold_seconds':.2}]}],
            'implementation':'HIDDEN ST', 'xml':'HIDDEN XML'}
        text = '\n'.join(response_summary('planner', value, {}))
        self.assertIn('startup.release', text)
        self.assertIn('button 释放，保持 0.2 秒', text)
        self.assertNotIn('HIDDEN ST', text)
        self.assertNotIn('HIDDEN XML', text)


if __name__=='__main__':unittest.main()
