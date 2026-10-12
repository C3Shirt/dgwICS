"""Small OpenAI-compatible JSON and image client; no SDK dependency."""

from __future__ import annotations

import base64
import http.client
import json
import mimetypes
import os
from pathlib import Path
import time
import urllib.error
import urllib.request

from .contracts import ContractError, require
from .provider_config import load_providers


class ModelServiceError(ContractError):
    """Provider/network failure; never treated as scene geometry or PLC logic feedback."""


class ModelOutputError(ContractError):
    """A truncated completion requires budget/input repair, not another identical generation."""


def reject_nonfinite(value):
    raise ValueError("nonfinite JSON number")


def response_format(config, role, payload):
    schema=config.get('response_schemas',{}).get(role)
    if schema is not None:
        require(isinstance(schema,dict) and schema.get('type')=='object','role response schema must describe an object')
        return {'type':'json_schema','json_schema':{'name':'FactoryIO_'+role,'strict':True,'schema':schema}}
    if role in config.get('schema_roles',[]):
        require(role=='vision','only visual review has an implemented response schema')
        from .response_schema import review_schema
        return {'type':'json_schema','json_schema':{'name':'FactoryIOReview','strict':True,'schema':review_schema(payload)}}
    return {'type':'json_object'} if config.get('json_mode',True) else None


class ModelClient:
    def __init__(self, config: dict):
        self.config = config
        self.providers = load_providers(Path(config["provider_config"]).resolve()) if config.get("provider_config") else {}
        self._exchange = {}
        self.progress_callback = None
        self.base_url = (config.get("base_url") or os.getenv("FACTORYIO_LLM_BASE_URL", "")).rstrip("/")
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}) if config.get("bypass_proxy", False) else urllib.request.ProxyHandler())

    def set_progress_callback(self, callback):
        previous = self.progress_callback
        self.progress_callback = callback
        return previous

    def activity(self, event, **details):
        if self.progress_callback: self.progress_callback({'event':event, **details})

    def read_stream(self, response, role):
        from .progress import PublicJSONStream, FIELD_LABELS
        parts=[]; packets=0; content_chars=0; data=[]; done=False; finish=None; first_packet=None; first_content=None
        started=time.monotonic()
        def preview(path, text):
            field=next((p for p in reversed(path) if isinstance(p,str)), '')
            self.activity('public_field',label=FIELD_LABELS.get(field,field),text=text,json_path=list(path))
        previewer=PublicJSONStream(preview)
        def consume(lines):
            nonlocal packets,content_chars,done,finish,first_packet,first_content
            text='\n'.join(lines)
            if text=='[DONE]': done=True; return
            try: chunk=json.loads(text,parse_constant=reject_nonfinite)
            except ValueError: raise ModelServiceError(f'{role} model returned an invalid stream packet') from None
            if not isinstance(chunk,dict) or chunk.get('error'):
                raise ModelServiceError(f'{role} model returned a stream error')
            packets+=1
            if first_packet is None: first_packet=round(time.monotonic()-started,6)
            for key,target in [('model','returned_model'),('id','response_id'),('usage','usage')]:
                if chunk.get(key) is not None: self._exchange[target]=chunk[key]
            choices=chunk.get('choices',[])
            require(isinstance(choices,list),f'{role} model stream choices must be an array')
            stage='response_connected'
            for choice in choices:
                require(isinstance(choice,dict),f'{role} model stream choice invalid')
                if choice.get('index',0)!=0: continue
                if choice.get('finish_reason') is not None: finish=choice['finish_reason']
                delta=choice.get('delta') or {}
                require(isinstance(delta,dict),f'{role} model stream delta invalid')
                content=delta.get('content')
                if isinstance(content,str) and content:
                    if first_content is None: first_content=round(time.monotonic()-started,6)
                    parts.append(content); content_chars+=len(content); previewer.feed(content); stage='receiving_output'
                elif delta.get('reasoning_content'): stage='provider_processing'
            self.activity('stream_activity',transport_stage=stage,
                content_chars=content_chars,packets=packets)
        try:
            for raw in response:
                try: line=raw.decode('utf-8').rstrip('\r\n').lstrip('\ufeff')
                except UnicodeError: raise ModelServiceError(f'{role} model stream encoding invalid') from None
                if not line:
                    if data: consume(data); data=[]
                    if done: break
                elif line.startswith('data:'): data.append(line[5:].lstrip(' '))
                # SSE comments and event/id fields do not contain completion text.
            if data and not done: consume(data)
        finally:
            self._exchange['raw_content']=''.join(parts)
            self._exchange['finish_reason']=finish
            self._exchange['stream_stats']={'packets':packets,'content_chars':content_chars,
                'done_received':done,'first_packet_seconds':first_packet,'first_content_seconds':first_content}
        if not done or finish is None:
            raise ModelServiceError(f'{role} model stream ended before a confirmed completion')
        return {'choices':[{'message':{'content':self._exchange['raw_content']},'finish_reason':finish}],
                'model':self._exchange.get('returned_model'),'id':self._exchange.get('response_id'),
                'usage':self._exchange.get('usage')}

    def complete(self, role: str, system: str, payload: dict, images: list[Path] | None = None) -> dict:
        self._exchange = {}
        provider_name = self.config.get("role_providers", {}).get(role)
        require(not self.providers or provider_name in self.providers, f"Model provider for {role} is not configured")
        provider = self.providers.get(provider_name, {})
        base_url = provider.get("base_url", self.base_url).rstrip("/")
        model = provider.get("model_name") or self.config.get("models", {}).get(role) or os.getenv("FACTORYIO_" + role.upper() + "_MODEL") \
            or os.getenv("FACTORYIO_LLM_MODEL")
        require(bool(base_url) and bool(model), f"Model for {role} is not configured: set base_url and models.{role}")
        content = [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, allow_nan=False)}]
        for path in images or []:
            mime = mimetypes.guess_type(str(path))[0]
            require(mime in ("image/png", "image/jpeg", "image/webp"), f"unsupported image: {path}")
            require(path.stat().st_size <= 20 * 1024 * 1024, "image exceeds 20 MiB")
            data = base64.b64encode(path.read_bytes()).decode("ascii")
            content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}})
        request_body = {"model": model, "messages": [{"role": "system", "content": system},
                       {"role": "user", "content": content if images else content[0]["text"]}]}
        provider_options = self.config.get('provider_options', {}).get(provider_name, {})
        role_options = self.config.get('request_options', {}).get(role, {})
        require(isinstance(provider_options,dict) and isinstance(role_options,dict),'request options must be objects')
        options = {**provider_options,**role_options}
        require(isinstance(options, dict) and not set(options) - {'max_tokens', 'temperature', 'thinking', 'enable_thinking'},
                'unsupported provider request options')
        if 'max_tokens' in options:
            require(type(options['max_tokens']) is int and 256 <= options['max_tokens'] <= 65536, 'invalid max_tokens')
        if 'thinking' in options:
            require(options['thinking'] in ({'type': 'disabled'}, {'type': 'enabled'}), 'invalid thinking option')
        if 'enable_thinking' in options:
            require(type(options['enable_thinking']) is bool, 'enable_thinking must be boolean')
        request_body.update(options)
        streaming=self.config.get('stream', self.progress_callback is not None)
        require(type(streaming) is bool, 'llm.stream must be boolean')
        if streaming:
            request_body.update(stream=True,stream_options={'include_usage':True})
        fmt=response_format(self.config,role,payload)
        if fmt is not None: request_body['response_format']=fmt
        headers = {"Content-Type": "application/json"}
        key = provider.get("api_key") if provider else os.getenv(self.config.get("api_key_env", "OPENAI_API_KEY"))
        if key:
            headers["Authorization"] = "Bearer " + key
        request = urllib.request.Request(base_url + "/chat/completions",
                                         json.dumps(request_body, ensure_ascii=False).encode("utf-8"), headers)
        self._exchange = {"endpoint": base_url + "/chat/completions", "requested_model": model, "provider": provider_name,
                          "request_options": options, 'response_format':request_body.get('response_format'),
                          'streaming_requested':streaming}
        self.activity('request_prepared',model=model,provider=provider_name,streaming=streaming)
        try:
            with self.opener.open(request, timeout=self.config.get("timeout_seconds", 90)) as response:
                self._exchange["http_status"] = getattr(response, "status", None)
                self.activity('response_connected',transport_stage='response_connected')
                content_type=getattr(response,'headers',{}).get('Content-Type','')
                self._exchange['response_mode']='sse' if 'text/event-stream' in content_type.lower() else 'json'
                body=self.read_stream(response,role) if self._exchange['response_mode']=='sse' else json.load(response)
        except urllib.error.HTTPError as exc:
            self._exchange["http_status"] = exc.code
            exc.close()
            # Do not echo response bodies which could contain proxy credentials/request headers.
            raise ModelServiceError(f"{role} model HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, http.client.HTTPException, ConnectionError) as exc:
            raise ModelServiceError(f"{role} model request failed ({type(exc).__name__})") from None
        try:
            text = body["choices"][0]["message"]["content"]
            self._exchange.update({"raw_content": text, "returned_model": body.get("model"),
                "response_id": body.get("id"), "usage": body.get("usage"),
                "finish_reason": body["choices"][0].get("finish_reason")})
            if self._exchange['finish_reason']=='length':
                raise ModelOutputError(f'{role} model output truncated (finish_reason=length); reduce evidence or increase completion budget')
            if isinstance(text, str) and text.strip().startswith("```"):
                text = "\n".join(text.strip().splitlines()[1:-1])
            result = json.loads(text, parse_constant=reject_nonfinite)
        except ModelOutputError:
            raise
        except (KeyError, IndexError, TypeError, ValueError):
            raise ContractError(f"{role} model did not return a JSON object") from None
        require(isinstance(result, dict), f"{role} model response must be object")
        return result

    def exchange_metadata(self) -> dict:
        """Completion text and transport metadata only; never auth headers or reasoning_content."""
        return dict(self._exchange)

    def secret_values(self) -> list[str]:
        return [provider["api_key"] for provider in self.providers.values()]
