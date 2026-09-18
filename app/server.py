import ast
import asyncio
import json
import os
import re
import uuid
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from jsonschema import Draft202012Validator

from app.browser import (
    BrowserBridge,
    BrowserBridgeError,
    BrowserInputUnavailableError,
    BrowserNotReadyError,
    BrowserResponseTimeout,
)

# One shared browser/profile. Requests are serialized by BrowserBridge.
_PROFILE_DIR = os.environ.get("DEEPSEEK_PROFILE_DIR", "./deepseek_user_data")
bridge = BrowserBridge(user_data_dir=_PROFILE_DIR)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await bridge.initialize()
    try:
        yield
    finally:
        await bridge.close()


app = FastAPI(lifespan=lifespan)


# ==========================================================
# TOOL PROTOCOL
# ==========================================================
TOOL_INSTRUCTION = """\
TOOL CALL RULES (JSON FIRST):
When a tool is needed, return one or more standalone JSON objects.
Do NOT wrap them in markdown/code fences.

Example:
{"name":"Write","input":{"file_path":"app/main.py","content":"def hello():\\n    print(\\"hello\\")\\n"}}

RULES:
1. "name" must exactly match a tool name supplied by the client.
2. "input" must be a JSON object matching that tool's input_schema.
3. For Write, preserve file content byte-for-byte in the JSON string semantics:
   keep every newline, blank line, space and tab. Do not reformat code.
4. For Edit, old_string must be copied EXACTLY from the current file. Never
   reconstruct it from memory and never normalize its indentation.
5. For Edit, new_string contains only the intended replacement.
6. Do not invent placeholder values such as "...".
7. Multiple tool calls may be emitted as multiple JSON objects.
8. If no tool is needed, answer normally in text.
"""

REPAIR_PROMPT = """\
The previous tool call was invalid or did not match its schema.
Return ONLY corrected standalone JSON tool call(s), with no markdown and no explanation.

Available tools: {tool_names}

Important:
- Write: preserve every newline, blank line, space and tab in file content.
- Edit: old_string MUST exactly match the current file. Copy it verbatim from
  the provided conversation/file context; do not reconstruct indentation.
- Do not use placeholders.
"""

FILE_REPAIR_PROMPT = """\
A file-edit tool call would produce invalid source.

Return ONLY the corrected JSON tool call, with no markdown or explanation.
Keep the user's intended change, but make the resulting file syntactically valid.

Tool error:
{error}

If the tool is Edit, preserve the exact current-file old_string and only change
new_string as necessary.
"""


# ==========================================================
# PROMPT BUILDING
# ==========================================================
def _normalize_system(system) -> str:
    if isinstance(system, list):
        return "\n".join(
            b.get("text", "")
            for b in system
            if isinstance(b, dict) and b.get("type") == "text"
        ).strip()
    return str(system or "").strip()


def compact_tools(tools) -> str:
    blocks = []
    for tool in tools:
        if not isinstance(tool, dict) or "name" not in tool:
            continue
        schema = json.dumps(
            tool.get("input_schema") or {},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        blocks.append(
            f"TÊN: {tool['name']}\n"
            f"MÔ TẢ: {(tool.get('description') or '').strip()}\n"
            f"INPUT_SCHEMA: {schema}"
        )
    return "\n\n".join(blocks)


MAX_TOOL_RESULT_CHARS = int(os.environ.get("MAX_TOOL_RESULT_CHARS", "10000"))
MAX_HISTORY_CHARS = int(os.environ.get("MAX_HISTORY_CHARS", "45000"))


def _compact_tool_result(value) -> str:
    text = value if isinstance(value, str) else str(value or "")
    if len(text) <= MAX_TOOL_RESULT_CHARS:
        return text
    head = MAX_TOOL_RESULT_CHARS * 2 // 3
    tail = MAX_TOOL_RESULT_CHARS - head
    return (
        text[:head]
        + f"\n\n[... bridge rút gọn {len(text) - MAX_TOOL_RESULT_CHARS} ký tự ...]\n\n"
        + text[-tail:]
    )


def _content_to_text(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content or "")

    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            parts.append(block.get("text", ""))
        elif block_type == "tool_use":
            parts.append(
                f"[TOOL_USE id={block.get('id', '')} name={block.get('name', '')}]\n"
                f"{json.dumps(block.get('input', {}), ensure_ascii=False)}"
            )
        elif block_type == "tool_result":
            result_content = block.get("content", "")
            if isinstance(result_content, list):
                result_content = "\n".join(
                    item.get("text", "")
                    for item in result_content
                    if isinstance(item, dict) and item.get("type") == "text"
                )
            status = "ERROR" if block.get("is_error") else "OK"
            parts.append(
                f"[TOOL_RESULT id={block.get('tool_use_id', '')} status={status}]\n"
                f"{_compact_tool_result(result_content)}"
            )
        else:
            parts.append(
                f"[CONTENT_BLOCK type={block_type}] "
                f"{json.dumps(block, ensure_ascii=False)}"
            )
    return "\n".join(part for part in parts if part)


def build_prompt(messages, tools, system_prompt, is_first_turn=None, tool_choice=None) -> str:
    sections = []

    if system_prompt:
        sections.append(f"HỆ THỐNG:\n{system_prompt}")

    if tools:
        sections.append(f"CÔNG CỤ KHẢ DỤNG:\n{compact_tools(tools)}")
        if tool_choice:
            sections.append(
                "LỰA CHỌN TOOL DO CLIENT YÊU CẦU:\n"
                + json.dumps(tool_choice, ensure_ascii=False, separators=(",", ":"))
            )
        sections.append(TOOL_INSTRUCTION.strip())

    transcript = []
    for index, message in enumerate(messages):
        role = str(message.get("role", "user")).lower()
        content = _content_to_text(message.get("content", ""))
        if role == "system":
            transcript.append(f"[SYSTEM INSTRUCTION AT TURN {index}]\n{content}")
        else:
            transcript.append(f"[{role.upper()}]\n{content}")

    transcript_text = "\n\n".join(transcript)
    if len(transcript_text) > MAX_HISTORY_CHARS:
        # Keep the latest context; old tool results are usually the largest
        # source of prompt growth.
        keep_tail = max(MAX_HISTORY_CHARS - 1800, 1000)
        transcript_text = (
            "[... lịch sử cũ đã được rút gọn để giảm latency ...]\n\n"
            + transcript_text[-keep_tail:]
        )

    sections.append(
        "LỊCH SỬ HỘI THOẠI:\n"
        "- SYSTEM INSTRUCTION áp dụng cho các lượt sau.\n"
        "- USER/ASSISTANT là nội dung hội thoại.\n"
        + transcript_text
    )
    sections.append("Hãy tạo phản hồi ASSISTANT tiếp theo.")
    return "\n\n".join(sections).strip()


# ==========================================================
# TOOL PARSER
# ==========================================================
OPEN_TAG = "<tool_call>"
CLOSE_TAG = "</tool_call>"
PARAM_RE = re.compile(r'<param\s+name\s*=\s*"([^"]+)"\s*>', re.IGNORECASE)
NAME_RE = re.compile(
    r'^\s*(?:name\s*[:=]\s*|<name>\s*)?["\']?'
    r'([A-Za-z_][A-Za-z0-9_-]*)["\']?\s*(?:</name>)?\s*$',
    re.MULTILINE,
)
PLACEHOLDER_VALUES = {"...", "…", "<giá trị>", "giá trị", "value", "gia tri"}

_JUNK_MARKER = re.compile(
    r"[｜|]{1,2}\s*(?:DSML|tool[▁_ ]?calls?)\s*[｜|]{1,2}\s*",
    re.IGNORECASE,
)
_NS_PREFIX = re.compile(
    r"(?<=<)(\s*/?\s*)(?:antml|anthropic|ds)\s*:\s*",
    re.IGNORECASE,
)


def _normalize_tool_syntax(text: str) -> str:
    if not text:
        return ""
    value = _JUNK_MARKER.sub("", text)
    value = _NS_PREFIX.sub(lambda match: match.group(1), value)
    value = re.sub(
        r"<\s*parameter\s+name\s*=\s*\"([^\"]+)\"\s*>",
        r'<param name="\1">',
        value,
        flags=re.I,
    )
    value = re.sub(r"<\s*/\s*parameter\s*>", "</param>", value, flags=re.I)
    value = re.sub(
        r"<\s*invoke\s+name\s*=\s*\"([^\"]+)\"\s*>",
        lambda match: f"{OPEN_TAG}\nname: {match.group(1)}\n",
        value,
        flags=re.I,
    )
    value = re.sub(
        r"<\s*/\s*(?:invoke|call|tool_use|function_call)\s*>",
        CLOSE_TAG,
        value,
        flags=re.I,
    )
    value = re.sub(
        r"<\s*/?\s*(?:calls|tool_calls|function_calls)\s*>",
        "",
        value,
        flags=re.I,
    )
    value = re.sub(
        r"<\s*(?:tool_use|function_call)\s*>",
        OPEN_TAG,
        value,
        flags=re.I,
    )
    return value


def _strip_fences_around_tags(text: str) -> str:
    text = re.sub(r"```[a-zA-Z]*\s*\n?(?=<tool_call>)", "", text)
    return re.sub(r"(?<=</tool_call>)\s*\n?```", "", text)


def _find_blocks(text: str):
    blocks = []
    index = 0
    length = len(text)

    while index < length:
        start = text.find(OPEN_TAG, index)
        if start == -1:
            break

        body_start = start + len(OPEN_TAG)
        depth = 1
        cursor = body_start
        closed = False

        while cursor < length:
            next_open = text.find(OPEN_TAG, cursor)
            next_close = text.find(CLOSE_TAG, cursor)
            if next_close == -1:
                break

            if next_open != -1 and next_open < next_close:
                depth += 1
                cursor = next_open + len(OPEN_TAG)
            else:
                depth -= 1
                if depth == 0:
                    end = next_close + len(CLOSE_TAG)
                    blocks.append((start, end, text[body_start:next_close]))
                    index = end
                    closed = True
                    break
                cursor = next_close + len(CLOSE_TAG)

        if closed:
            continue

        next_open = text.find(OPEN_TAG, body_start)
        end = next_open if next_open != -1 else length
        body = text[body_start:end]
        if PARAM_RE.search(body) or re.search(r"^\s*name\s*:", body, re.MULTILINE):
            blocks.append((start, end, body))
        index = end if end > start else start + len(OPEN_TAG)

    return blocks


def _match_name(raw_name, valid_tool_names):
    if not raw_name:
        return None
    raw = str(raw_name).strip()
    return next(
        (name for name in valid_tool_names if name.lower() == raw.lower()),
        None,
    )


def _parse_param_blocks(body: str):
    matches = list(PARAM_RE.finditer(body))
    if not matches:
        return None

    output = {}
    for index, match in enumerate(matches):
        segment_start = match.end()
        segment_end = (
            matches[index + 1].start()
            if index + 1 < len(matches)
            else len(body)
        )
        segment = body[segment_start:segment_end]
        close = segment.rfind("</param>")
        value = segment[:close] if close != -1 else segment

        if "\n" in value.strip():
            if value.startswith("\n"):
                value = value[1:]
            if value.endswith("\n"):
                value = value[:-1]
        else:
            value = value.strip()

        output[match.group(1)] = value
    return output


def _repair_json(value: str) -> str:
    output = []
    in_string = False
    escaped = False

    for index, char in enumerate(value):
        if not in_string:
            output.append(char)
            if char == '"':
                in_string = True
            continue

        if escaped:
            output.append(char)
            escaped = False
        elif char == "\\":
            output.append(char)
            escaped = True
        elif char == '"':
            next_index = index + 1
            while next_index < len(value) and value[next_index] in " \t\r\n":
                next_index += 1
            if next_index >= len(value) or value[next_index] in ",}]:":
                output.append('"')
                in_string = False
            else:
                output.append('\\"')
        elif char == "\n":
            output.append("\\n")
        elif char == "\r":
            output.append("\\r")
        elif char == "\t":
            output.append("\\t")
        else:
            output.append(char)

    return "".join(output)


def _extract_balanced_json(text: str, start: int = 0):
    depth = 0
    began = None
    in_string = False
    escaped = False

    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                began = index
            depth += 1
        elif char == "}":
            if depth:
                depth -= 1
                if depth == 0 and began is not None:
                    return text[began:index + 1]
    return None


def _parse_json_body(body: str):
    raw = _extract_balanced_json(body)
    if not raw:
        return None

    for candidate in (raw, _repair_json(raw)):
        try:
            return json.loads(candidate, strict=False)
        except json.JSONDecodeError:
            pass
    return None


def _parse_legacy_xml(body: str):
    params = dict(
        re.findall(
            r'<parameter\s+name="([^"]+)"\s*>\s*([\s\S]*?)\s*</parameter>',
            body,
        )
    )
    if params:
        return params

    params = dict(
        re.findall(r"<([A-Za-z_][A-Za-z0-9_]*)>\s*([\s\S]*?)\s*</\1>", body)
    )
    params.pop("name", None)
    return params or None


def _looks_like_placeholder(tool_input: dict) -> bool:
    values = [
        value for value in (tool_input or {}).values()
        if isinstance(value, str)
    ]
    if not values:
        return False
    return all(
        value.strip().strip('"').lower() in PLACEHOLDER_VALUES
        for value in values
    )


def coerce_types(tool_input: dict, schema: dict) -> dict:
    properties = (schema or {}).get("properties") or {}
    output = {}

    for key, value in (tool_input or {}).items():
        if not isinstance(value, str):
            output[key] = value
            continue

        expected_type = (properties.get(key) or {}).get("type")
        stripped = value.strip()

        try:
            if expected_type == "integer":
                output[key] = int(float(stripped))
            elif expected_type == "number":
                output[key] = float(stripped)
            elif expected_type == "boolean":
                output[key] = stripped.lower() in ("true", "1", "yes", "có")
            elif expected_type in ("array", "object"):
                output[key] = json.loads(stripped)
            else:
                output[key] = value
        except Exception:
            output[key] = value

    return output


def _schema_errors(tool_input: dict, schema: dict) -> list[str]:
    if not schema:
        return []

    try:
        Draft202012Validator.check_schema(schema)
        errors = sorted(
            Draft202012Validator(schema).iter_errors(tool_input),
            key=lambda error: list(error.absolute_path),
        )
    except Exception as exc:
        return [f"input_schema không hợp lệ: {exc}"]

    return [
        f"{'.'.join(str(part) for part in error.absolute_path) or '<root>'}: "
        f"{error.message}"
        for error in errors
    ]


def _extract_json_tool_calls(raw_reply: str, tools: list):
    valid = {
        tool["name"]: (tool.get("input_schema") or {})
        for tool in tools
        if isinstance(tool, dict) and "name" in tool
    }

    calls = []
    spans = []
    cursor = 0

    while cursor < len(raw_reply):
        raw = _extract_balanced_json(raw_reply, cursor)
        if not raw:
            break

        start = raw_reply.find(raw, cursor)
        cursor = start + len(raw)

        try:
            data = json.loads(raw, strict=False)
        except json.JSONDecodeError:
            try:
                data = json.loads(_repair_json(raw), strict=False)
            except json.JSONDecodeError:
                continue

        if not isinstance(data, dict):
            continue

        name = data.get("name") or data.get("tool") or data.get("tool_name")
        matched = _match_name(name, list(valid))
        if not matched:
            continue

        tool_input = data.get(
            "input",
            data.get("arguments", data.get("parameters")),
        )
        if not isinstance(tool_input, dict):
            tool_input = {
                key: value
                for key, value in data.items()
                if key not in {
                    "name", "tool", "tool_name",
                    "input", "arguments", "parameters",
                }
            }

        tool_input = coerce_types(tool_input, valid[matched])
        if (
            _schema_errors(tool_input, valid[matched])
            or _looks_like_placeholder(tool_input)
        ):
            continue

        calls.append({"name": matched, "input": tool_input})
        spans.append((start, start + len(raw)))

    return calls, spans


def extract_tool_calls(raw_reply: str, tools: list):
    """Parse JSON first, then preserve legacy XML compatibility."""
    raw_reply = raw_reply or ""

    json_calls, json_spans = _extract_json_tool_calls(raw_reply, tools)
    if json_calls:
        text_content = raw_reply
        for start, end in sorted(json_spans, reverse=True):
            text_content = text_content[:start] + text_content[end:]
        print(f"🔧 JSON tools: {[call['name'] for call in json_calls]}")
        return text_content.strip(), json_calls

    valid_names = [
        tool.get("name")
        for tool in tools
        if isinstance(tool, dict) and "name" in tool
    ]
    schemas = {
        tool["name"]: (tool.get("input_schema") or {})
        for tool in tools
        if isinstance(tool, dict) and "name" in tool
    }

    cleaned = _strip_fences_around_tags(_normalize_tool_syntax(raw_reply))
    calls = []
    spans = []

    for start, end, body in _find_blocks(cleaned):
        head = body.split("<param", 1)[0].split("{", 1)[0]
        matched = None

        for match in NAME_RE.finditer(head):
            matched = _match_name(match.group(1), valid_names)
            if matched:
                break

        tool_input = None
        params = _parse_param_blocks(body)
        if params is not None and matched:
            tool_input = coerce_types(params, schemas.get(matched, {}))

        if tool_input is None:
            data = _parse_json_body(body)
            if isinstance(data, dict):
                name = data.get("name") or data.get("tool") or data.get("tool_name")
                matched = matched or _match_name(name, valid_names)
                if matched:
                    tool_input = (
                        data.get("input")
                        or data.get("arguments")
                        or data.get("parameters")
                    )
                    if isinstance(tool_input, str):
                        tool_input = _parse_json_body(tool_input) or {}
                    if not isinstance(tool_input, dict):
                        tool_input = {
                            key: value
                            for key, value in data.items()
                            if key not in {
                                "name", "tool", "tool_name",
                                "input", "arguments", "parameters",
                            }
                        }
                    tool_input = coerce_types(
                        tool_input,
                        schemas.get(matched, {}),
                    )

        if tool_input is None and matched:
            legacy = _parse_legacy_xml(body)
            if legacy is not None:
                tool_input = coerce_types(
                    legacy,
                    schemas.get(matched, {}),
                )

        if not matched or tool_input is None:
            spans.append((start, end))
            continue

        errors = _schema_errors(tool_input, schemas.get(matched, {}))
        if errors or _looks_like_placeholder(tool_input):
            spans.append((start, end))
            continue

        calls.append({"name": matched, "input": tool_input})
        spans.append((start, end))

    text_content = cleaned
    for start, end in sorted(spans, reverse=True):
        text_content = text_content[:start] + text_content[end:]

    return text_content.strip(), calls


# ==========================================================
# FILE/EDIT SAFETY
# ==========================================================
def _path_from_tool_input(tool_input: dict) -> Optional[Path]:
    path = tool_input.get("file_path") if isinstance(tool_input, dict) else None
    if not isinstance(path, str) or not path.strip():
        return None
    try:
        return Path(path).expanduser().resolve()
    except OSError:
        return None


def _normalize_code_line(line: str) -> str:
    # Used ONLY for locating an Edit target; never used as replacement content.
    return re.sub(r"\s+", " ", line.strip())


def _repair_edit_old_string(file_text: str, old_string: str):
    """Return the exact file substring matching old_string despite whitespace-only drift.

    The repair is conservative:
    - exact match wins;
    - otherwise a unique contiguous line match is accepted;
    - finally, a unique whitespace-collapsed match is accepted.
    Ambiguous matches are never changed.
    """
    if not isinstance(old_string, str) or not old_string:
        return old_string, False

    if old_string in file_text:
        return old_string, False

    old_lines = old_string.splitlines()
    if not old_lines:
        return old_string, False

    actual_lines = file_text.splitlines(keepends=True)
    wanted = [_normalize_code_line(line) for line in old_lines]

    # Preserve the distinction between a real multi-line edit and a one-line edit.
    if len(wanted) >= 2:
        matches = []
        width = len(wanted)
        for index in range(0, len(actual_lines) - width + 1):
            candidate = [
                _normalize_code_line(line)
                for line in actual_lines[index:index + width]
            ]
            if candidate == wanted:
                matches.append(index)

        if len(matches) == 1:
            index = matches[0]
            return "".join(actual_lines[index:index + width]), True
        if len(matches) > 1:
            return old_string, False

    # Handles the common failure where the model collapses newlines into spaces.
    collapsed_old = re.sub(r"\s+", "", old_string)
    if len(collapsed_old) < 24:
        return old_string, False

    matches = []
    for start in range(len(actual_lines)):
        compact = ""
        for end in range(start, len(actual_lines)):
            compact += re.sub(r"\s+", "", actual_lines[end])
            if compact == collapsed_old:
                matches.append((start, end + 1))
                break
            if len(compact) > len(collapsed_old):
                break

    if len(matches) == 1:
        start, end = matches[0]
        return "".join(actual_lines[start:end]), True

    return old_string, False


def _validate_source_text(path: Path, content: str) -> Optional[str]:
    suffix = path.suffix.lower()
    try:
        if suffix == ".py":
            ast.parse(content, filename=str(path))
        elif suffix == ".json":
            json.loads(content)
    except (SyntaxError, IndentationError, json.JSONDecodeError) as exc:
        line = getattr(exc, "lineno", "?")
        column = getattr(exc, "offset", "?")
        return f"{path}: dòng {line}, cột {column}: {exc}"
    return None


def _repair_and_validate_tool_calls(tool_calls: list):
    """Repair whitespace-drifted Edit targets and validate resulting files.

    Returns (error, repaired_any). Never forwards an Edit whose resulting local
    file is syntactically invalid when the target file is readable.
    """
    repaired_any = False

    for call in tool_calls:
        name = call.get("name")
        data = call.get("input") or {}
        path = _path_from_tool_input(data)
        if path is None:
            continue

        if name == "Write":
            content = data.get("content")
            if isinstance(content, str):
                error = _validate_source_text(path, content)
                if error:
                    return error, repaired_any

        elif name == "Edit":
            old_string = data.get("old_string")
            new_string = data.get("new_string")

            if not isinstance(old_string, str) or not isinstance(new_string, str):
                continue

            try:
                current = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                # The actual client/tool will report a useful error. Do not guess.
                continue

            repaired_old, changed = _repair_edit_old_string(current, old_string)
            if changed:
                data["old_string"] = repaired_old
                repaired_any = True
                print(f"🩹 Đã sửa whitespace drift trong Edit: {path}")

            if data["old_string"] not in current:
                return (
                    f"{path}: old_string không khớp nội dung file hiện tại. "
                    "Không thể sửa tự động vì không tìm thấy một match duy nhất.",
                    repaired_any,
                )

            candidate = current.replace(
                data["old_string"],
                new_string,
                1,
            )
            error = _validate_source_text(path, candidate)
            if error:
                return error, repaired_any

    return None, repaired_any


# ==========================================================
# REQUEST VALIDATION / RESPONSE HELPERS
# ==========================================================
MAX_REQUEST_BYTES = int(
    os.environ.get("MAX_REQUEST_BYTES", str(2 * 1024 * 1024))
)
BRIDGE_API_KEY = os.environ.get("BRIDGE_API_KEY")


def _error_response(
    message: str,
    status_code: int,
    error_type: str = "invalid_request_error",
):
    return JSONResponse(
        status_code=status_code,
        content={
            "type": "error",
            "error": {"type": error_type, "message": message},
        },
    )


def _check_auth(request: Request):
    if not BRIDGE_API_KEY:
        return None

    supplied = request.headers.get("x-api-key")
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        supplied = authorization[7:]

    if supplied != BRIDGE_API_KEY:
        return _error_response(
            "API key không hợp lệ.",
            401,
            "authentication_error",
        )
    return None


def _validate_request_body(body) -> Optional[str]:
    if not isinstance(body, dict):
        return "Request body phải là một JSON object."

    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return "messages phải là một danh sách không rỗng."

    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            return f"messages[{index}] phải là object."

        if message.get("role") not in {"user", "assistant", "system"}:
            return (
                f"messages[{index}].role={message.get('role')!r} không được hỗ trợ; "
                "role hợp lệ: user, assistant, system."
            )

        content = message.get("content")
        if not isinstance(content, (str, list)):
            return (
                f"messages[{index}].content phải là chuỗi "
                "hoặc danh sách content block."
            )

        if isinstance(content, list) and not all(
            isinstance(block, dict) for block in content
        ):
            return (
                f"Mọi content block trong messages[{index}] "
                "phải là object."
            )

    system = body.get("system", "")
    if not isinstance(system, (str, list)):
        return "system phải là chuỗi hoặc danh sách text block."
    if isinstance(system, list) and not all(
        isinstance(block, dict) for block in system
    ):
        return "Mọi system block phải là object."

    tools = body.get("tools", [])
    if not isinstance(tools, list):
        return "tools phải là một danh sách."

    seen_names = set()
    for index, tool in enumerate(tools):
        if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
            return f"tools[{index}] phải có name dạng chuỗi."

        if tool["name"] in seen_names:
            return f"Tên tool bị trùng: {tool['name']}."
        seen_names.add(tool["name"])

        schema = tool.get("input_schema", {})
        if not isinstance(schema, dict):
            return f"tools[{index}].input_schema phải là object."

        try:
            Draft202012Validator.check_schema(schema)
        except Exception as exc:
            return f"tools[{index}].input_schema không hợp lệ: {exc}"

    if "max_tokens" in body and (
        type(body["max_tokens"]) is not int or body["max_tokens"] <= 0
    ):
        return "max_tokens phải là số nguyên dương."

    if "stream" in body and not isinstance(body["stream"], bool):
        return "stream phải là boolean."

    if "stop_sequences" in body and (
        not isinstance(body["stop_sequences"], list)
        or not all(isinstance(item, str) for item in body["stop_sequences"])
    ):
        return "stop_sequences phải là một danh sách chuỗi."

    if "model" in body and not isinstance(body["model"], str):
        return "model phải là chuỗi."

    if "tool_choice" in body and not isinstance(body["tool_choice"], dict):
        return "tool_choice phải là object."

    if isinstance(body.get("tool_choice"), dict):
        choice_type = body["tool_choice"].get("type", "auto")
        if choice_type not in {"auto", "any", "tool", "none"}:
            return "tool_choice.type phải là auto, any, tool hoặc none."
        if choice_type == "tool":
            choice_name = body["tool_choice"].get("name")
            if choice_name not in seen_names:
                return "tool_choice.name phải trùng với một tool đã khai báo."

    return None


def estimate_tokens(value) -> int:
    serialized = (
        value
        if isinstance(value, str)
        else json.dumps(value, ensure_ascii=False)
    )
    return max((len(serialized.encode("utf-8")) + 3) // 4, 1)


async def _read_json_body(request: Request):
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_REQUEST_BYTES:
                return None, _error_response("Request body quá lớn.", 413)
        except ValueError:
            return None, _error_response(
                "Content-Length không hợp lệ.",
                400,
            )

    try:
        body = await request.json()
    except ValueError:
        return None, _error_response(
            "Request body không phải JSON hợp lệ.",
            400,
        )

    if len(json.dumps(body, ensure_ascii=False).encode("utf-8")) > MAX_REQUEST_BYTES:
        return None, _error_response("Request body quá lớn.", 413)

    validation_error = _validate_request_body(body)
    if validation_error:
        return None, _error_response(validation_error, 400)

    return body, None


def _truncate_at_stop(text: str, stop_sequences):
    matches = [
        (text.find(sequence), sequence)
        for sequence in stop_sequences or []
        if isinstance(sequence, str)
        and sequence
        and text.find(sequence) >= 0
    ]
    if not matches:
        return text, None

    index, sequence = min(matches, key=lambda item: item[0])
    return text[:index], sequence


def _truncate_to_token_budget(text: str, max_tokens: int) -> str:
    if estimate_tokens(text) <= max_tokens:
        return text

    low, high = 0, len(text)
    while low < high:
        midpoint = (low + high + 1) // 2
        if estimate_tokens(text[:midpoint]) <= max_tokens:
            low = midpoint
        else:
            high = midpoint - 1
    return text[:low]


def _select_tools(tools: list, tool_choice) -> list:
    if not isinstance(tool_choice, dict):
        return tools

    choice_type = tool_choice.get("type", "auto")
    if choice_type == "none":
        return []
    if choice_type == "tool":
        return [
            tool
            for tool in tools
            if tool.get("name") == tool_choice.get("name")
        ]
    return tools


# ==========================================================
# REQUEST PROCESSING
# ==========================================================
async def _process_request(body: dict):
    requested_model = body.get("model", "deepseek-web")
    messages = body["messages"]
    tools = _select_tools(
        body.get("tools", []),
        body.get("tool_choice"),
    )
    system_prompt = _normalize_system(body.get("system", ""))

    prompt_to_send = build_prompt(
        messages,
        tools,
        system_prompt,
        tool_choice=body.get("tool_choice"),
    )

    print(
        "🔒 Gửi request lên DeepSeek Web "
        f"(model client yêu cầu: {requested_model})..."
    )

    async with bridge.conversation(reset=True):
        ds_reply = await bridge.send_message_to_deepseek(prompt_to_send)
        text_content, tool_calls = extract_tool_calls(ds_reply, tools)

        normalized_reply = _normalize_tool_syntax(ds_reply or "")
        has_possible_tool = bool(_find_blocks(normalized_reply))

        if (
            tools
            and not tool_calls
            and not has_possible_tool
            and re.search(
                r'["\'](?:name|tool|tool_name)["\']\s*:',
                ds_reply or "",
            )
        ):
            has_possible_tool = True

        validation_error, repaired = _repair_and_validate_tool_calls(tool_calls)

        if repaired:
            # The repair changes only the structured tool input sent back to
            # Claude Code; no source file is written by the bridge.
            validation_error, _ = _repair_and_validate_tool_calls(tool_calls)

        if validation_error:
            has_possible_tool = True

        if tools and (not tool_calls or has_possible_tool):
            print("🔁 Tool call cần sửa; yêu cầu DeepSeek gửi lại một lần...")
            names = ", ".join(tool["name"] for tool in tools)

            if validation_error:
                repair_prompt = FILE_REPAIR_PROMPT.format(
                    error=validation_error,
                )
            else:
                repair_prompt = REPAIR_PROMPT.format(tool_names=names)

            retry_reply = await bridge.send_message_to_deepseek(repair_prompt)
            retry_text, retry_calls = extract_tool_calls(
                retry_reply,
                tools,
            )

            retry_error, retry_repaired = _repair_and_validate_tool_calls(
                retry_calls
            )

            if retry_repaired:
                retry_error, _ = _repair_and_validate_tool_calls(retry_calls)

            if retry_calls and not retry_error:
                tool_calls = retry_calls
                text_content = text_content or retry_text
            else:
                # Never forward a known-bad file operation.
                if validation_error or retry_error:
                    tool_calls = []

    stop_sequence = None
    if not tool_calls:
        text_content, stop_sequence = _truncate_at_stop(
            text_content,
            body.get("stop_sequences", []),
        )

    max_tokens = body.get("max_tokens")
    stop_reason = (
        "tool_use"
        if tool_calls
        else ("stop_sequence" if stop_sequence else "end_turn")
    )

    if max_tokens and estimate_tokens(text_content) > max_tokens:
        text_content = _truncate_to_token_budget(
            text_content,
            max_tokens,
        )
        stop_reason = "max_tokens"

    for tool_call in tool_calls:
        tool_call["id"] = f"toolu_{uuid.uuid4().hex[:24]}"

    output_for_usage = {
        "text": text_content,
        "tools": [
            {
                "name": call["name"],
                "input": call["input"],
            }
            for call in tool_calls
        ],
    }

    return {
        "requested_model": requested_model,
        "prompt": prompt_to_send,
        "text": text_content,
        "tool_calls": tool_calls,
        "stop_reason": stop_reason,
        "stop_sequence": stop_sequence,
        "output_tokens": estimate_tokens(output_for_usage),
    }


def _content_blocks(result):
    blocks = []

    if result["text"]:
        blocks.append({
            "type": "text",
            "text": result["text"],
        })

    blocks.extend(
        {
            "type": "tool_use",
            "id": tool_call["id"],
            "name": tool_call["name"],
            "input": tool_call["input"],
        }
        for tool_call in result["tool_calls"]
    )

    return blocks or [
        {
            "type": "text",
            "text": "(không có nội dung)",
        }
    ]


def _sse(event, payload):
    return (
        f"event: {event}\n"
        f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
    )


# ==========================================================
# ENDPOINTS
# ==========================================================
@app.get("/health")
async def health():
    ready = await bridge.check_ready()
    return {
        "status": "ok" if ready else "degraded",
        "bridge": "deepseek-claude-agent",
        "browser_ready": ready,
        "busy": bridge.lock.locked(),
        "last_error": None if ready else bridge.last_error,
    }


@app.post("/v1/messages/count_tokens")
async def count_tokens(request: Request):
    auth_error = _check_auth(request)
    if auth_error:
        return auth_error

    body, error = await _read_json_body(request)
    if error:
        return error

    prompt = build_prompt(
        body["messages"],
        _select_tools(
            body.get("tools", []),
            body.get("tool_choice"),
        ),
        _normalize_system(body.get("system", "")),
        tool_choice=body.get("tool_choice"),
    )
    return {"input_tokens": estimate_tokens(prompt)}


@app.post("/v1/messages")
async def anthropic_adapter(request: Request):
    auth_error = _check_auth(request)
    if auth_error:
        return auth_error

    body, error = await _read_json_body(request)
    if error:
        return error

    cooldown_error = bridge.cooldown_error()
    if cooldown_error:
        return _error_response(
            cooldown_error,
            424,
            "api_error",
        )

    if not body.get("stream", False):
        try:
            result = await _process_request(body)
        except BrowserResponseTimeout as exc:
            return _error_response(
                str(exc),
                504,
                "timeout_error",
            )
        except BrowserInputUnavailableError as exc:
            return _error_response(
                str(exc),
                424,
                "api_error",
            )
        except BrowserNotReadyError as exc:
            if bridge.cooldown_error():
                return _error_response(
                    str(exc),
                    424,
                    "api_error",
                )
            return _error_response(
                str(exc),
                503,
                "service_unavailable_error",
            )
        except BrowserBridgeError as exc:
            return _error_response(
                str(exc),
                502,
                "api_error",
            )
        except Exception as exc:
            print(f"❌ Lỗi bridge ngoài dự kiến: {exc}")
            return _error_response(
                "Bridge gặp lỗi nội bộ.",
                500,
                "api_error",
            )

        return {
            "id": f"msg_{uuid.uuid4().hex[:24]}",
            "type": "message",
            "role": "assistant",
            "model": result["requested_model"],
            "content": _content_blocks(result),
            "stop_reason": result["stop_reason"],
            "stop_sequence": result["stop_sequence"],
            "usage": {
                "input_tokens": estimate_tokens(result["prompt"]),
                "output_tokens": result["output_tokens"],
            },
        }

    async def event_generator():
        message_id = f"msg_{uuid.uuid4().hex[:24]}"
        prompt_preview = build_prompt(
            body["messages"],
            _select_tools(
                body.get("tools", []),
                body.get("tool_choice"),
            ),
            _normalize_system(body.get("system", "")),
            tool_choice=body.get("tool_choice"),
        )

        yield _sse(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": message_id,
                    "type": "message",
                    "role": "assistant",
                    "model": body.get("model", "deepseek-web"),
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {
                        "input_tokens": estimate_tokens(prompt_preview),
                        "output_tokens": 0,
                    },
                },
            },
        )

        task = asyncio.create_task(_process_request(body))
        try:
            while not task.done():
                done, _ = await asyncio.wait({task}, timeout=5)
                if task not in done:
                    yield ": ping\n\n"
            result = await task

        except asyncio.CancelledError:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            raise

        except Exception as exc:
            error_type = (
                "timeout_error"
                if isinstance(exc, BrowserResponseTimeout)
                else "api_error"
            )
            message = (
                str(exc)
                if isinstance(exc, BrowserBridgeError)
                else "Bridge gặp lỗi nội bộ."
            )
            if not isinstance(exc, BrowserBridgeError):
                print(f"❌ Lỗi stream ngoài dự kiến: {exc}")

            yield _sse(
                "error",
                {
                    "type": "error",
                    "error": {
                        "type": error_type,
                        "message": message,
                    },
                },
            )
            return

        block_index = 0

        if result["text"]:
            yield _sse(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": block_index,
                    "content_block": {
                        "type": "text",
                        "text": "",
                    },
                },
            )

            for index in range(0, len(result["text"]), 256):
                yield _sse(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": block_index,
                        "delta": {
                            "type": "text_delta",
                            "text": result["text"][index:index + 256],
                        },
                    },
                )

            yield _sse(
                "content_block_stop",
                {
                    "type": "content_block_stop",
                    "index": block_index,
                },
            )
            block_index += 1

        for tool_call in result["tool_calls"]:
            yield _sse(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": block_index,
                    "content_block": {
                        "type": "tool_use",
                        "id": tool_call["id"],
                        "name": tool_call["name"],
                        "input": {},
                    },
                },
            )

            payload = json.dumps(
                tool_call["input"],
                ensure_ascii=False,
            )

            for index in range(0, len(payload), 512):
                yield _sse(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": block_index,
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": payload[index:index + 512],
                        },
                    },
                )

            yield _sse(
                "content_block_stop",
                {
                    "type": "content_block_stop",
                    "index": block_index,
                },
            )
            block_index += 1

        yield _sse(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {
                    "stop_reason": result["stop_reason"],
                    "stop_sequence": result["stop_sequence"],
                },
                "usage": {
                    "output_tokens": result["output_tokens"],
                },
            },
        )
        yield _sse("message_stop", {"type": "message_stop"})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
