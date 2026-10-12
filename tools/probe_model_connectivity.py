"""Probe configured text and image chat APIs without logging credentials.

Reads the small scalar-mapping LLM_Config.yaml format used by this workspace.
No SDK/YAML/image dependency and no Factory I/O or PLC mutations.
"""

from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import random
import re
import secrets
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib


def load_scalar_config(path: Path) -> dict:
    """Accept only two-level string mappings; never execute YAML tags or print source lines."""
    result, section = {}, None
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"( *)([A-Za-z][A-Za-z0-9_]*)\s*:\s*(.*?)\s*", line)
        if not match:
            raise ValueError(f"Unsupported scalar YAML format at line {number}")
        indent, key, value = match.groups()
        if not indent:
            if value:
                raise ValueError(f"Expected a section at line {number}")
            section = key
            if section in result:
                raise ValueError(f"Duplicate section at line {number}")
            result[section] = {}
        else:
            if section is None or key in result[section]:
                raise ValueError(f"Invalid or duplicate field at line {number}")
            if value.startswith('"'):
                value = json.loads(value)
            elif value.startswith("'") and value.endswith("'"):
                value = value[1:-1].replace("''", "'")
            else:
                value = value.split(" #", 1)[0].strip()
            if not isinstance(value, str) or not value:
                raise ValueError(f"Empty string field at line {number}")
            result[section][key.lower()] = value
    for name in ("DeepSeek", "AliyunLLM"):
        if name not in result or not all(result[name].get(k) for k in ("base_url", "model_name", "api_key")):
            raise ValueError(f"Required fields missing for {name}")
        u = urllib.parse.urlsplit(result[name]["base_url"])
        if u.scheme != "https" or not u.hostname or u.username or u.password or u.query or u.fragment:
            raise ValueError(f"Expected credential-free HTTPS base_url for {name}")
    return result


def scrub(value, api_keys: list[str]):
    if isinstance(value, str):
        for key in api_keys:
            value = value.replace(key, "<redacted>")
        value = re.sub(r"\bsk-[A-Za-z0-9_-]+", "<redacted>", value)
        value = re.sub(r"(?i)bearer\s+\S+", "Bearer <redacted>", value)
        value = re.sub(r"data:image/[^;]+;base64,[A-Za-z0-9+/=]+", "<image-data-withheld>", value)
        return value
    if isinstance(value, dict):
        return {str(k): scrub(v, api_keys) for k, v in value.items() if str(k).lower() not in {"api_key", "authorization", "headers"}}
    if isinstance(value, list):
        return [scrub(v, api_keys) for v in value]
    return value


def parse_content(content) -> dict | None:
    if not isinstance(content, str):
        return None
    text = content.strip()
    if text.startswith("```"):
        text = "\n".join(text.splitlines()[1:-1])
    try:
        result = json.loads(text)
        return result if isinstance(result, dict) else None
    except (ValueError, TypeError):
        return None


def request_chat(provider: dict, messages: list[dict], timeout: float, use_proxy: bool) -> dict:
    endpoint = provider["base_url"].rstrip("/") + "/chat/completions"
    body = {"model": provider["model_name"], "messages": messages,
            "response_format": {"type": "json_object"}, "max_tokens": 1024, "stream": False}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler() if use_proxy else urllib.request.ProxyHandler({}))
    request = urllib.request.Request(endpoint, json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                     {"Content-Type": "application/json", "Authorization": "Bearer " + provider["api_key"]}, method="POST")
    start = time.monotonic()
    result = {"endpoint": endpoint, "requested_model": provider["model_name"], "transport": "configured_proxy" if use_proxy else "direct",
              "response_format_requested": "json_object", "request_completed": False}
    try:
        with opener.open(request, timeout=timeout) as response:
            result["http_status"] = response.status
            data = json.load(response)
        choice = data.get("choices", [{}])[0]
        content = choice.get("message", {}).get("content")
        result.update({"request_completed": True, "returned_model": data.get("model"),
                       "response_id": data.get("id"), "finish_reason": choice.get("finish_reason"),
                       "usage": data.get("usage"), "content": content, "parsed": parse_content(content)})
    except urllib.error.HTTPError as error:
        result.update({"http_status": error.code, "error_type": "HTTPError"})
        try:
            data = json.loads(error.read(128 * 1024).decode("utf-8", errors="replace"))
            detail = data.get("error", data)
            if isinstance(detail, dict):
                result["service_error"] = {k: detail[k] for k in ("code", "type", "message") if k in detail}
        except (ValueError, TypeError):
            result["service_error"] = "non-JSON response withheld"
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError, IndexError, OSError) as error:
        result.update({"error_type": type(error).__name__, "error_detail": "request or response parsing failed; raw error withheld"})
    result["latency_seconds"] = round(time.monotonic() - start, 3)
    return result


def make_fixture(path: Path) -> list[dict]:
    """Create a new, known-ground-truth PNG test fixture; no source image is edited."""
    width, height = 600, 240
    colors = random.SystemRandom().sample(["red", "green", "blue"], 3)
    shapes = random.SystemRandom().sample(["square", "circle", "triangle"], 3)
    palette = {"red": (225, 35, 35), "green": (25, 170, 65), "blue": (35, 75, 220)}
    pixels = bytearray([255]) * (width * height * 3)
    for cx, color, shape in zip([100, 300, 500], colors, shapes):
        for y in range(60, 181):
            for x in range(cx - 60, cx + 61):
                inside = (abs(x - cx) <= 55 and 65 <= y <= 175) if shape == "square" else \
                         ((x - cx) ** 2 + (y - 120) ** 2 <= 58 ** 2) if shape == "circle" else \
                         (60 <= y <= 180 and abs(x - cx) <= (y - 60) / 2)
                if inside:
                    offset = (y * width + x) * 3
                    pixels[offset:offset+3] = bytes(palette[color])
    scanlines = b"".join(b"\x00" + pixels[y*width*3:(y+1)*width*3] for y in range(height))
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) \
          + chunk(b"IDAT", zlib.compress(scanlines)) + chunk(b"IEND", b"")
    path.write_bytes(png)
    return [{"color": color, "shape": shape} for color, shape in zip(colors, shapes)]


def image_messages(prompt: str, path: Path) -> list[dict]:
    mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    data_url = "data:" + mime + ";base64," + base64.b64encode(path.read_bytes()).decode("ascii")
    return [{"role": "system", "content": "Inspect the attached image. Return only the requested JSON object. Do not infer runtime behavior from a still image."},
            {"role": "user", "content": [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": data_url}}]}]


def image_info(path: Path) -> dict:
    data = path.read_bytes()
    return {"path": str(path.resolve()), "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("LLM_Config.yaml"))
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--scene", type=Path)
    parser.add_argument("--aliyun-model", help="explicit model-ID override for diagnosis; does not modify the YAML")
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--use-proxy", action="store_true", help="use environment proxy instead of direct HTTPS")
    args = parser.parse_args()
    config = load_scalar_config(args.config)
    configured_aliyun_model = config["AliyunLLM"]["model_name"]
    if args.aliyun_model:
        config["AliyunLLM"]["model_name"] = args.aliyun_model
    if args.out.exists():
        raise SystemExit("Use a new --out directory; existing probe evidence will not be overwritten")
    args.out.mkdir(parents=True)
    keys = [p["api_key"] for p in config.values()]
    def save(name: str, value) -> None:
        (args.out / name).write_text(json.dumps(scrub(value, keys), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report = {"started_at_utc": datetime.now(timezone.utc).isoformat(), "config_path": str(args.config.resolve()),
              "credential_source": "explicit provider fields from local YAML; no OPENAI_API_KEY fallback",
              "configured_aliyun_model": configured_aliyun_model, "aliyun_model_override": args.aliyun_model,
              "factoryio_or_plc_mutated": False, "tests": {}}
    nonce = secrets.token_hex(12)
    prompt = f'Return only a JSON object with exactly these values: {{"token":"{nonce}","sum":13}}.'
    messages = [{"role": "system", "content": "Follow the test instruction exactly and return JSON."}, {"role": "user", "content": prompt}]
    print("Probing DeepSeek and Aliyun text requests (credentials withheld)...", flush=True)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {name: executor.submit(request_chat, config[name], messages, args.timeout, args.use_proxy) for name in ("DeepSeek", "AliyunLLM")}
        for name, future in futures.items():
            r = future.result()
            r["passed"] = r.get("parsed") == {"token": nonce, "sum": 13}
            report["tests"][name + "_text"] = r
            save(name + "-text.json", r)
            print(json.dumps({"test": name + "_text", "passed": r["passed"], "http_status": r.get("http_status"), "latency_seconds": r["latency_seconds"], "error_type": r.get("error_type")}), flush=True)
    if report["tests"]["AliyunLLM_text"].get("request_completed"):
        fixture_path = args.out / "vision-fixture.png"
        expected = make_fixture(fixture_path)
        save("vision-fixture-expected.json", {"objects_left_to_right": expected, "image": image_info(fixture_path)})
        vision_prompt = 'Describe the three colored objects in the image from left to right. Return {"objects_left_to_right":[{"color":"red|green|blue","shape":"square|circle|triangle"}, ...]}. Select one English color and shape per object. The prompt does not specify their actual order or pairing.'
        print("Probing Aliyun image input against independently generated visual ground truth...", flush=True)
        r = request_chat(config["AliyunLLM"], image_messages(vision_prompt, fixture_path), args.timeout, args.use_proxy)
        r.update({"image": image_info(fixture_path), "expected": expected,
                  "passed": isinstance(r.get("parsed"), dict) and r["parsed"].get("objects_left_to_right") == expected})
        report["tests"]["AliyunLLM_visual_fixture"] = r
        save("AliyunLLM-visual-fixture.json", r)
        print(json.dumps({"test": "AliyunLLM_visual_fixture", "passed": r["passed"], "http_status": r.get("http_status"), "latency_seconds": r["latency_seconds"], "error_type": r.get("error_type")}), flush=True)
        if args.scene and args.scene.is_file():
            scene_prompt = ('只根据附图回答，返回JSON：{"has_conveyors":true或false,"fence_color":"英文颜色词或unknown",'
                            '"parts_palette_side":"left或right或none或unknown","description_zh":"两三句描述能看到的设备与布局"}。'
                            '不要把静态图片当作设备已运行或工艺通过的证明。')
            print("Probing Aliyun with an existing Factory I/O screenshot (not a live capture)...", flush=True)
            r = request_chat(config["AliyunLLM"], image_messages(scene_prompt, args.scene), args.timeout, args.use_proxy)
            parsed = r.get("parsed") or {}
            r.update({"image": image_info(args.scene), "historical_screenshot": True,
                      "check_scope": "visible scene recognition only; not runtime or process acceptance",
                      "passed": parsed.get("has_conveyors") is True and parsed.get("fence_color") == "blue" and parsed.get("parts_palette_side") == "right"})
            report["tests"]["AliyunLLM_factoryio_image"] = r
            save("AliyunLLM-factoryio-image.json", r)
            print(json.dumps({"test": "AliyunLLM_factoryio_image", "passed": r["passed"], "http_status": r.get("http_status"), "latency_seconds": r["latency_seconds"], "error_type": r.get("error_type")}), flush=True)
    report["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
    report["text_models_communicate"] = all(report["tests"][n + "_text"].get("passed") for n in ("DeepSeek", "AliyunLLM"))
    report["aliyun_image_input_verified"] = report["tests"].get("AliyunLLM_visual_fixture", {}).get("passed", False)
    report["factoryio_image_recognition_verified"] = report["tests"].get("AliyunLLM_factoryio_image", {}).get("passed", False)
    save("report.json", report)
    print(json.dumps({"report": str((args.out / "report.json").resolve()),
                      "text_models_communicate": report["text_models_communicate"],
                      "aliyun_image_input_verified": report["aliyun_image_input_verified"],
                      "factoryio_image_recognition_verified": report["factoryio_image_recognition_verified"]}), flush=True)


if __name__ == "__main__":
    main()
