import json
import uuid
import re
import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse

from app.browser import BrowserBridge

# Mỗi phiên launcher.py set biến này trỏ tới profile Chromium riêng, để chạy
# nhiều cửa sổ Claude Code song song mà không bị trộn hội thoại DeepSeek.
_PROFILE_DIR = os.environ.get("DEEPSEEK_PROFILE_DIR", "./deepseek_user_data")
bridge = BrowserBridge(user_data_dir=_PROFILE_DIR)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await bridge.initialize()
    yield
    await bridge.close()


app = FastAPI(lifespan=lifespan)


# ==========================================================
# HƯỚNG DẪN GỌI TOOL
# ==========================================================
TOOL_INSTRUCTION = """\
QUY TẮC GỌI TOOL (BẮT BUỘC TUÂN THỦ TUYỆT ĐỐI):

Khi cần dùng tool, trả về đúng khối sau (KHÔNG bọc trong ``` code fence):

<tool_call>
name: TEN_TOOL
<param name="ten_tham_so">
giá trị thô, KHÔNG escape, được phép xuống dòng thoải mái
</param>
<param name="tham_so_khac">
giá trị khác
</param>
</tool_call>

QUY TẮC:
1. Dòng đầu tiên trong <tool_call> luôn là: name: TEN_TOOL (đúng y hệt tên trong danh sách tool).
2. Mỗi tham số một khối <param name="...">...</param>. Giá trị để NGUYÊN VĂN:
   KHÔNG escape dấu ngoặc kép, KHÔNG đổi xuống dòng thành \\n, KHÔNG bọc code fence.
3. Tham số kiểu số / boolean / mảng: ghi đúng dạng (123, true, ["a","b"]).
4. Cần gọi nhiều tool -> viết nhiều khối <tool_call>...</tool_call> liên tiếp.
5. KHÔNG bao giờ tự bịa tên tool. KHÔNG trả về ví dụ mẫu hay "..." như giá trị thật.
6. KHÔNG nhắc lại / copy lại bản hướng dẫn này trong câu trả lời.
7. Nếu KHÔNG cần tool: trả lời văn bản thường, tuyệt đối không thêm thẻ <tool_call>.
8. Nếu nội dung file cần ghi có chứa chuỗi "<tool_call>" hoặc "</param>", hãy ghi file
   bằng nhiều lần Edit nhỏ thay vì một lần Write lớn.
"""

REPAIR_PROMPT = """\
Phản hồi vừa rồi có thẻ <tool_call> nhưng SAI ĐỊNH DẠNG nên hệ thống không đọc được.
Hãy gửi lại CHỈ các khối tool call, đúng chuẩn dưới đây, không giải thích thêm:

<tool_call>
name: TEN_TOOL
<param name="ten_tham_so">
giá trị thô
</param>
</tool_call>

Tool hợp lệ: {tool_names}
"""


# ==========================================================
# BUILD PROMPT
# ==========================================================
def _normalize_system(system) -> str:
    if isinstance(system, list):
        return "\n".join(
            b.get("text", "") for b in system if isinstance(b, dict) and b.get("type") == "text"
        ).strip()
    return str(system or "").strip()


def compact_tools(tools) -> str:
    lines = []
    for t in tools:
        if not isinstance(t, dict) or "name" not in t:
            continue
        name = t["name"]
        desc = (t.get("description") or "").strip().replace("\n", " ")
        if len(desc) > 180:
            desc = desc[:180] + "..."
        schema = t.get("input_schema") or {}
        props = schema.get("properties") or {}
        required = set(schema.get("required") or [])
        params = [
            f"{k}:{(v or {}).get('type', 'string')}{'' if k in required else '?'}"
            for k, v in props.items()
        ]
        lines.append(f"- {name}({', '.join(params)})\n    {desc}")
    return "\n".join(lines)


def build_prompt(messages, tools, system_prompt, is_first_turn: bool) -> str:
    last_msg = messages[-1]
    current_turn_content = ""

    if isinstance(last_msg.get("content"), list):
        for block in last_msg["content"]:
            btype = block.get("type")
            if btype == "text":
                current_turn_content += block.get("text", "") + "\n"
            elif btype == "tool_result":
                content = block.get("content")
                if isinstance(content, list):
                    content = "\n".join(
                        c.get("text", "") for c in content if isinstance(c, dict)
                    )
                status = "LỖI" if block.get("is_error") else "OK"
                current_turn_content += f"\n[KẾT QUẢ TOOL ({status})]:\n{content}\n"
            elif btype == "tool_use":
                current_turn_content += (
                    f"\n[BẠN VỪA GỌI TOOL '{block.get('name')}' VỚI INPUT: "
                    f"{json.dumps(block.get('input', {}), ensure_ascii=False)}]\n"
                )
    else:
        current_turn_content = str(last_msg.get("content", ""))

    current_turn_content = current_turn_content.strip()

    if not tools:
        return current_turn_content

    if is_first_turn:
        prefix = f"HỆ THỐNG:\n{system_prompt}\n\n" if system_prompt else ""
        return (
            f"{prefix}"
            f"CÔNG CỤ KHẢ DỤNG:\n{compact_tools(tools)}\n\n"
            f"{TOOL_INSTRUCTION}\n"
            f"LỆNH CỦA NGƯỜI DÙNG:\n{current_turn_content}"
        )

    tool_names = ", ".join(t.get("name", "") for t in tools if isinstance(t, dict))
    return (
        f"{current_turn_content}\n\n"
        f"[NHẮC LẠI] Tool khả dụng: {tool_names}\n"
        f"{TOOL_INSTRUCTION}"
    )


# ==========================================================
# PARSER  (mỗi hàm chỉ định nghĩa ĐÚNG MỘT LẦN ở đây)
# ==========================================================
OPEN_TAG = "<tool_call>"
CLOSE_TAG = "</tool_call>"
PARAM_RE = re.compile(r'<param\s+name\s*=\s*"([^"]+)"\s*>', re.IGNORECASE)
NAME_RE = re.compile(
    r'^\s*(?:name\s*[:=]\s*|<name>\s*)?["\']?([A-Za-z_][A-Za-z0-9_-]*)["\']?\s*(?:</name>)?\s*$',
    re.MULTILINE,
)
PLACEHOLDER_VALUES = {"...", "…", "<giá trị>", "giá trị", "value", "gia tri"}

# Token đặc biệt của DeepSeek: ｜ là U+FF5C, ▁ là U+2581 — không phải ASCII.
_JUNK_MARKER = re.compile(r"[｜|]{1,2}\s*(?:DSML|tool[▁_ ]?calls?)\s*[｜|]{1,2}\s*", re.IGNORECASE)
_NS_PREFIX = re.compile(r"(?<=<)\s*(?:/\s*)?(?:antml|anthropic|ds)\s*:\s*", re.IGNORECASE)


def _normalize_tool_syntax(text: str) -> str:
    """
    DeepSeek hay trôi về định dạng function-call gốc của nó:
        <｜｜DSML｜｜ calls> / <｜｜DSML｜｜ invoke name="Read"> / <｜｜DSML｜｜ parameter ...>
    Dịch về <tool_call>/<param> trước khi parse.
    """
    if not text:
        return ""

    s = _JUNK_MARKER.sub("", text)
    s = _NS_PREFIX.sub("", s)

    s = re.sub(r"<\s*parameter\s+name\s*=\s*\"([^\"]+)\"\s*>", r'<param name="\1">', s, flags=re.I)
    s = re.sub(r"<\s*/\s*parameter\s*>", "</param>", s, flags=re.I)

    s = re.sub(
        r"<\s*invoke\s+name\s*=\s*\"([^\"]+)\"\s*>",
        lambda m: f"{OPEN_TAG}\nname: {m.group(1)}\n",
        s,
        flags=re.I,
    )
    s = re.sub(r"<\s*/\s*(?:invoke|call|tool_use|function_call)\s*>", CLOSE_TAG, s, flags=re.I)
    s = re.sub(r"<\s*/?\s*(?:calls|tool_calls|function_calls)\s*>", "", s, flags=re.I)
    s = re.sub(r"<\s*(?:tool_use|function_call)\s*>", OPEN_TAG, s, flags=re.I)

    return s


def _strip_fences_around_tags(text: str) -> str:
    text = re.sub(r"```[a-zA-Z]*\s*\n?(?=<tool_call>)", "", text)
    text = re.sub(r"(?<=</tool_call>)\s*\n?```", "", text)
    return text


def _find_blocks(text: str):
    """
    Đếm độ sâu lồng nhau + TỰ ĐÓNG block khi DeepSeek quên </tool_call>
    (cắt tại <tool_call> kế tiếp, hoặc hết chuỗi).
    """
    blocks = []
    i, n = 0, len(text)
    while i < n:
        start = text.find(OPEN_TAG, i)
        if start == -1:
            break
        body_start = start + len(OPEN_TAG)
        depth = 1
        j = body_start
        closed = False
        while j < n:
            nxt_open = text.find(OPEN_TAG, j)
            nxt_close = text.find(CLOSE_TAG, j)
            if nxt_close == -1:
                break
            if nxt_open != -1 and nxt_open < nxt_close:
                depth += 1
                j = nxt_open + len(OPEN_TAG)
            else:
                depth -= 1
                if depth == 0:
                    blocks.append((start, nxt_close + len(CLOSE_TAG), text[body_start:nxt_close]))
                    j = nxt_close + len(CLOSE_TAG)
                    closed = True
                    break
                j = nxt_close + len(CLOSE_TAG)

        if closed:
            i = j
            continue

        nxt_open = text.find(OPEN_TAG, body_start)
        end = nxt_open if nxt_open != -1 else n
        body = text[body_start:end]
        if PARAM_RE.search(body) or re.search(r"^\s*name\s*:", body, re.MULTILINE):
            blocks.append((start, end, body))
        i = end if end > start else start + len(OPEN_TAG)

    return blocks


def _match_name(raw_name, valid_tool_names):
    if not raw_name:
        return None
    raw = str(raw_name).strip()
    return next((n for n in valid_tool_names if n.lower() == raw.lower()), None)


def _parse_param_blocks(body: str):
    matches = list(PARAM_RE.finditer(body))
    if not matches:
        return None
    out = {}
    for idx, m in enumerate(matches):
        seg_start = m.end()
        seg_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(body)
        seg = body[seg_start:seg_end]
        close = seg.rfind("</param>")
        value = seg[:close] if close != -1 else seg

        if "\n" in value.strip():
            # nhiều dòng = nội dung file -> giữ NGUYÊN VĂN
            value = value[1:] if value.startswith("\n") else value
            value = value[:-1] if value.endswith("\n") else value
        else:
            # một dòng = path/pattern/command -> phải trim
            value = value.strip()

        out[m.group(1)] = value
    return out


def _repair_json(s: str) -> str:
    out = []
    in_str = False
    esc = False
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if not in_str:
            out.append(ch)
            if ch == '"':
                in_str = True
        else:
            if esc:
                out.append(ch)
                esc = False
            elif ch == "\\":
                out.append(ch)
                esc = True
            elif ch == '"':
                j = i + 1
                while j < n and s[j] in " \t\r\n":
                    j += 1
                if j >= n or s[j] in ",}]:":
                    out.append('"')
                    in_str = False
                else:
                    out.append('\\"')
            elif ch == "\n":
                out.append("\\n")
            elif ch == "\r":
                out.append("\\r")
            elif ch == "\t":
                out.append("\\t")
            else:
                out.append(ch)
        i += 1
    return "".join(out)


def _extract_balanced_json(text: str, start: int = 0):
    depth = 0
    began = None
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                began = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and began is not None:
                    return text[began:i + 1]
    return None


def _parse_json_body(body: str):
    raw = _extract_balanced_json(body)
    if not raw:
        return None
    for candidate in (raw, _repair_json(raw)):
        try:
            return json.loads(candidate, strict=False)
        except json.JSONDecodeError:
            continue
    return None


def _parse_legacy_xml(body: str):
    params = dict(re.findall(r'<parameter\s+name="([^"]+)"\s*>\s*([\s\S]*?)\s*</parameter>', body))
    if params:
        return params
    params = dict(re.findall(r"<([A-Za-z_][A-Za-z0-9_]*)>\s*([\s\S]*?)\s*</\1>", body))
    params.pop("name", None)
    return params or None


def _looks_like_placeholder(tool_input: dict) -> bool:
    if not tool_input:
        return False
    vals = [v for v in tool_input.values() if isinstance(v, str)]
    if not vals:
        return False
    return all(v.strip().strip('"').lower() in PLACEHOLDER_VALUES for v in vals)


def coerce_types(tool_input: dict, schema: dict) -> dict:
    props = (schema or {}).get("properties") or {}
    out = {}
    for k, v in (tool_input or {}).items():
        if not isinstance(v, str):
            out[k] = v
            continue
        t = (props.get(k) or {}).get("type")
        s = v.strip()
        try:
            if t == "integer":
                out[k] = int(float(s))
            elif t == "number":
                out[k] = float(s)
            elif t == "boolean":
                out[k] = s.lower() in ("true", "1", "yes", "có")
            elif t in ("array", "object"):
                out[k] = json.loads(s)
            else:
                out[k] = v
        except Exception:
            out[k] = v
    return out


def extract_tool_calls(raw_reply: str, tools: list):
    valid_tool_names = [t.get("name") for t in tools if isinstance(t, dict) and "name" in t]
    schema_by_name = {
        t["name"]: (t.get("input_schema") or {})
        for t in tools
        if isinstance(t, dict) and "name" in t
    }

    cleaned = _strip_fences_around_tags(_normalize_tool_syntax(raw_reply or ""))
    tool_calls = []
    spans = []

    for start, end, body in _find_blocks(cleaned):
        head = body.split("<param", 1)[0].split("{", 1)[0]
        matched = None
        for m in NAME_RE.finditer(head):
            matched = _match_name(m.group(1), valid_tool_names)
            if matched:
                break

        tool_input = None

        params = _parse_param_blocks(body)
        if params is not None and matched:
            tool_input = coerce_types(params, schema_by_name.get(matched, {}))

        if tool_input is None:
            data = _parse_json_body(body)
            if isinstance(data, dict):
                name = data.get("name") or data.get("tool") or data.get("tool_name")
                matched = matched or _match_name(name, valid_tool_names)
                if matched:
                    ti = data.get("input") or data.get("arguments") or data.get("parameters")
                    if isinstance(ti, str):
                        ti = _parse_json_body(ti) or {}
                    if not isinstance(ti, dict):
                        ti = {
                            k: v for k, v in data.items()
                            if k not in ("name", "tool", "tool_name", "input", "arguments", "parameters")
                        }
                    tool_input = coerce_types(ti, schema_by_name.get(matched, {}))

        if tool_input is None and matched:
            legacy = _parse_legacy_xml(body)
            if legacy is not None:
                tool_input = coerce_types(legacy, schema_by_name.get(matched, {}))

        if not matched or tool_input is None:
            spans.append((start, end))
            continue

        if _looks_like_placeholder(tool_input):
            print(f"↩️  Bỏ qua tool '{matched}' vì input chỉ là ví dụ mẫu.")
            spans.append((start, end))
            continue

        tool_calls.append({"name": matched, "input": tool_input})
        spans.append((start, end))
        print(f"🔧 Tool: {matched} | keys: {list(tool_input.keys())}")

    text_content = cleaned
    for start, end in sorted(spans, reverse=True):
        text_content = text_content[:start] + text_content[end:]

    return text_content.strip(), tool_calls


# ==========================================================
# ENDPOINTS
# ==========================================================
@app.get("/health")
async def health():
    return {
        "status": "ok",
        "bridge": "deepseek-claude-agent",
        "browser_ready": bridge.page is not None,
        "profile_dir": _PROFILE_DIR,
    }


@app.post("/v1/messages/count_tokens")
async def count_tokens(request: Request):
    body = await request.json()
    approx = len(json.dumps(body, ensure_ascii=False)) // 4
    return {"input_tokens": max(approx, 1)}


@app.post("/v1/messages")
async def anthropic_adapter(request: Request):
    body = await request.json()
    requested_model = body.get("model", "claude-3-5-sonnet-20241022")
    is_stream = body.get("stream", False)
    messages = body.get("messages", [])
    if not messages:
        raise HTTPException(status_code=400, detail="No messages provided")

    tools = [t for t in body.get("tools", []) if isinstance(t, dict) and "name" in t]
    system_prompt = _normalize_system(body.get("system", ""))
    # Không dùng len(messages) == 1: sai khi Claude Code gửi kèm system riêng
    # hoặc khi lịch sử bị compact. Turn đầu thật sự = chưa có message nào
    # role="assistant" trong lịch sử.
    is_first_turn = not any(m.get("role") == "assistant" for m in messages)

    prompt_to_send = build_prompt(messages, tools, system_prompt, is_first_turn)

    print(f"🔒 Bắn lệnh lên DeepSeek (Model: {requested_model})...")
    ds_reply = await bridge.send_message_to_deepseek(prompt_to_send)

    text_content, tool_calls = extract_tool_calls(ds_reply, tools)

    if tools and not tool_calls and OPEN_TAG in (ds_reply or ""):
        print("🔁 Format sai, yêu cầu DeepSeek gửi lại tool call...")
        names = ", ".join(t["name"] for t in tools)
        retry_reply = await bridge.send_message_to_deepseek(
            REPAIR_PROMPT.format(tool_names=names)
        )
        retry_text, retry_calls = extract_tool_calls(retry_reply, tools)
        if retry_calls:
            tool_calls = retry_calls
            text_content = text_content or retry_text

    is_tool_call = len(tool_calls) > 0
    stop_reason = "tool_use" if is_tool_call else "end_turn"

    for tc in tool_calls:
        tc["id"] = f"toolu_{uuid.uuid4().hex[:24]}"

    out_tokens = max(len(text_content) // 4, 1)

    if not is_stream:
        content_blocks = []
        if text_content:
            content_blocks.append({"type": "text", "text": text_content})
        for tc in tool_calls:
            content_blocks.append({
                "type": "tool_use",
                "id": tc["id"],
                "name": tc["name"],
                "input": tc["input"],
            })
        if not content_blocks:
            content_blocks.append({"type": "text", "text": "(không có nội dung)"})
        return {
            "id": f"msg_{uuid.uuid4().hex[:24]}",
            "type": "message",
            "role": "assistant",
            "model": requested_model,
            "content": content_blocks,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": len(prompt_to_send) // 4, "output_tokens": out_tokens},
        }

    async def event_generator():
        msg_id = f"msg_{uuid.uuid4().hex[:24]}"

        def sse(event, payload):
            return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

        yield sse("message_start", {
            "type": "message_start",
            "message": {
                "id": msg_id, "type": "message", "role": "assistant",
                "model": requested_model, "content": [],
                "stop_reason": None, "stop_sequence": None,
                "usage": {"input_tokens": len(prompt_to_send) // 4, "output_tokens": 0},
            },
        })

        block_idx = 0
        if text_content:
            yield sse("content_block_start", {
                "type": "content_block_start", "index": block_idx,
                "content_block": {"type": "text", "text": ""},
            })
            for i in range(0, len(text_content), 64):
                yield sse("content_block_delta", {
                    "type": "content_block_delta", "index": block_idx,
                    "delta": {"type": "text_delta", "text": text_content[i:i + 64]},
                })
                await asyncio.sleep(0.005)
            yield sse("content_block_stop", {"type": "content_block_stop", "index": block_idx})
            block_idx += 1

        for tc in tool_calls:
            yield sse("content_block_start", {
                "type": "content_block_start", "index": block_idx,
                "content_block": {"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": {}},
            })
            payload = json.dumps(tc["input"], ensure_ascii=False)
            for i in range(0, len(payload), 512):
                yield sse("content_block_delta", {
                    "type": "content_block_delta", "index": block_idx,
                    "delta": {"type": "input_json_delta", "partial_json": payload[i:i + 512]},
                })
            yield sse("content_block_stop", {"type": "content_block_stop", "index": block_idx})
            block_idx += 1

        yield sse("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": out_tokens},
        })
        yield sse("message_stop", {"type": "message_stop"})

    return StreamingResponse(event_generator(), media_type="text/event-stream")