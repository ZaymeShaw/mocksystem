#!/usr/bin/env python3
"""Minimal Anthropic-compatible reverse proxy that logs full request bodies."""
from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse
import uvicorn

UPSTREAM = os.environ.get("LLM_GATEWAY_UPSTREAM", "https://dashscope.aliyuncs.com/apps/anthropic").rstrip("/")
LOG_DIR = Path(os.environ.get("LLM_GATEWAY_LOG_DIR", Path(__file__).resolve().parent / "logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "llm_calls.jsonl"
HOST = os.environ.get("LLM_GATEWAY_HOST", "127.0.0.1")
PORT = int(os.environ.get("LLM_GATEWAY_PORT", "4000"))

app = FastAPI(title="eval-llm-gateway-thin")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append(rec: dict) -> None:
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")


def _forward_headers(req: Request) -> dict:
    hop = {"host", "content-length", "connection", "transfer-encoding"}
    return {k: v for k, v in req.headers.items() if k.lower() not in hop}


from callbacks.case_id import detect_protocol, extract_case_id, strip_eval_case_id_from_body


@app.get("/health")
async def health():
    return {"ok": True, "upstream": UPSTREAM, "log_file": str(LOG_FILE)}


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy(path: str, request: Request):
    call_id = str(uuid.uuid4())
    t0 = time.time()
    body = await request.body()
    url = f"{UPSTREAM}/{path}"
    if request.url.query:
        url += f"?{request.url.query}"

    req_json = None
    if body:
        try:
            req_json = json.loads(body)
        except Exception:
            req_json = {"_raw": body[:2000].decode("utf-8", "replace")}

    headers = _forward_headers(request)
    is_stream = bool(isinstance(req_json, dict) and req_json.get("stream"))

    case_id, case_source, case_warnings = extract_case_id(
        headers={k: v for k, v in request.headers.items()},
        body=req_json if isinstance(req_json, dict) else None,
    )
    protocol = detect_protocol(path="/" + path, body=req_json if isinstance(req_json, dict) else None)
    # Strip custom field before upstream; keep original in log
    forward_body = body
    if isinstance(req_json, dict) and (
        "eval_case_id" in req_json
        or (isinstance(req_json.get("metadata"), dict) and "eval_case_id" in req_json["metadata"])
    ):
        stripped = strip_eval_case_id_from_body(req_json)
        forward_body = json.dumps(stripped, ensure_ascii=False).encode("utf-8")
        # Content-Length will be wrong if forwarded blindly — drop it
        headers = {k: v for k, v in headers.items() if k.lower() != "content-length"}

    rec = {
        "event": "request",
        "ts": _now(),
        "call_id": call_id,
        "case_id": case_id,
        "case_id_source": case_source,
        "protocol": protocol,
        "method": request.method,
        "path": "/" + path,
        "model": (req_json or {}).get("model") if isinstance(req_json, dict) else None,
        "stream": is_stream,
        "request": req_json,
    }
    if case_warnings:
        rec["case_id_warnings"] = case_warnings
    _append(rec)

    client = httpx.AsyncClient(timeout=None)
    try:
        if is_stream:
            upstream = await client.send(
                client.build_request(request.method, url, headers=headers, content=forward_body),
                stream=True,
            )

            async def gen():
                chunks: list[bytes] = []
                try:
                    async for chunk in upstream.aiter_bytes():
                        chunks.append(chunk)
                        yield chunk
                finally:
                    text = b"".join(chunks).decode("utf-8", "replace")
                    _append(
                        {
                            "event": "response_stream",
                            "ts": _now(),
                            "call_id": call_id,
                            "status_code": upstream.status_code,
                            "latency_ms": int((time.time() - t0) * 1000),
                            "response_text": text[:500000],
                        }
                    )
                    await upstream.aclose()
                    await client.aclose()

            out_headers = {
                k: v
                for k, v in upstream.headers.items()
                if k.lower() not in {"content-encoding", "transfer-encoding", "content-length"}
            }
            return StreamingResponse(
                gen(),
                status_code=upstream.status_code,
                headers=out_headers,
                media_type=upstream.headers.get("content-type"),
            )

        upstream = await client.request(request.method, url, headers=headers, content=forward_body)
        resp_body = upstream.content
        try:
            resp_json = json.loads(resp_body)
        except Exception:
            resp_json = {"_raw": resp_body[:20000].decode("utf-8", "replace")}
        _append(
            {
                "event": "response",
                "ts": _now(),
                "call_id": call_id,
                "status_code": upstream.status_code,
                "latency_ms": int((time.time() - t0) * 1000),
                "response": resp_json,
            }
        )
        out_headers = {
            k: v
            for k, v in upstream.headers.items()
            if k.lower() not in {"content-encoding", "transfer-encoding", "content-length"}
        }
        await client.aclose()
        return Response(
            content=resp_body,
            status_code=upstream.status_code,
            headers=out_headers,
            media_type=upstream.headers.get("content-type"),
        )
    except Exception as e:
        _append({"event": "error", "ts": _now(), "call_id": call_id, "error": str(e)})
        await client.aclose()
        raise


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args()
    print(f"[llm-gateway-thin] upstream={UPSTREAM}")
    print(f"[llm-gateway-thin] log={LOG_FILE}")
    print(f"[llm-gateway-thin] listen=http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
