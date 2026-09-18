import json

from app.server import (
    _normalize_tool_syntax,
    _select_tools,
    _validate_request_body,
    build_prompt,
    estimate_tokens,
    extract_tool_calls,
)


TOOLS = [
    {
        "name": "Write",
        "description": "Write a file",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["file_path", "content"],
            "additionalProperties": False,
        },
    }
]


def test_build_prompt_keeps_system_without_tools():
    prompt = build_prompt(
        [{"role": "user", "content": "hello"}],
        [],
        "Always answer briefly.",
    )
    assert "Always answer briefly." in prompt
    assert "hello" in prompt


def test_build_prompt_keeps_complete_history_and_schema():
    messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
        {"role": "user", "content": "third"},
    ]
    prompt = build_prompt(messages, TOOLS, "system")
    assert all(value in prompt for value in ("first", "second", "third", "system"))
    assert '"required": [' in prompt
    assert "file_path" in prompt


def test_mid_conversation_system_message_is_preserved_as_instruction():
    messages = [
        {"role": "user", "content": "inspect the project"},
        {"role": "system", "content": "Use concise output from now on."},
        {"role": "assistant", "content": "Understood."},
        {"role": "user", "content": "continue"},
    ]
    assert _validate_request_body({"messages": messages}) is None
    prompt = build_prompt(messages, [], "Initial system prompt")
    assert "[SYSTEM INSTRUCTION AT TURN 1]" in prompt
    assert "Use concise output from now on." in prompt
    assert "cùng mức ưu tiên" in prompt


def test_extract_tool_call_and_coerce_schema_types():
    tools = [
        {
            "name": "Run",
            "input_schema": {
                "type": "object",
                "properties": {"count": {"type": "integer"}},
                "required": ["count"],
            },
        }
    ]
    text, calls = extract_tool_calls(
        '<tool_call>\nname: Run\n<param name="count">2</param>\n</tool_call>',
        tools,
    )
    assert text == ""
    assert calls == [{"name": "Run", "input": {"count": 2}}]


def test_invalid_tool_input_is_rejected():
    text, calls = extract_tool_calls(
        '<tool_call>\nname: Write\n<param name="file_path">a.txt</param>\n</tool_call>',
        TOOLS,
    )
    assert text == ""
    assert calls == []


def test_namespaced_closing_tag_stays_a_closing_tag():
    normalized = _normalize_tool_syntax(
        '<antml:tool_use>\nname: Write\n</antml:tool_use>'
    )
    assert normalized.count("<tool_call>") == 1
    assert normalized.count("</tool_call>") == 1


def test_request_validation_rejects_bad_messages_and_schema():
    assert _validate_request_body({"messages": []})
    error = _validate_request_body(
        {
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"name": "bad", "input_schema": {"type": "not-a-type"}}],
        }
    )
    assert error and "input_schema" in error


def test_tool_choice_restricts_or_disables_tools():
    assert _select_tools(TOOLS, {"type": "tool", "name": "Write"}) == TOOLS
    assert _select_tools(TOOLS, {"type": "none"}) == []
    error = _validate_request_body(
        {
            "messages": [{"role": "user", "content": "hi"}],
            "tools": TOOLS,
            "tool_choice": {"type": "tool", "name": "Missing"},
        }
    )
    assert error and "tool_choice.name" in error


def test_token_estimate_counts_utf8_and_json():
    assert estimate_tokens("xin chào") >= 2
    assert estimate_tokens({"message": "hello"}) == estimate_tokens(
        json.dumps({"message": "hello"}, ensure_ascii=False)
    )
