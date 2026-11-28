"""Normalize, validate and resume tool approvals and interactive interrupts."""

import json
from typing import TypedDict

class UserAnswer(TypedDict):
    answer: str


DEFAULT_DECISIONS = ["approve", "edit", "reject"]


def pending_interrupts(snapshot):
    items = list(getattr(snapshot, "interrupts", ()) or ())
    for task in getattr(snapshot, "tasks", ()) or ():
        items.extend(getattr(task, "interrupts", ()) or ())
    return list({getattr(item, "id", None) or str(index): item for index, item in enumerate(items)}.values())


def _allowed(schema):
    found = []
    if isinstance(schema, dict):
        prop = schema.get("properties", {}).get("type", {})
        found.extend([prop["const"]] if "const" in prop else prop.get("enum", []))
        for value in schema.values():
            found.extend(_allowed(value))
    elif isinstance(schema, list):
        for value in schema:
            found.extend(_allowed(value))
    return [kind for kind in dict.fromkeys(found) if kind in {"approve", "edit", "reject", "respond"}]


def normalize_interrupt(item, index=0):
    value = item.value
    schema = getattr(item, "response_schema", None)
    if hasattr(schema, "model_json_schema"):
        schema = schema.model_json_schema()
    request = {"id": getattr(item, "id", None) or f"interrupt-{index}", "raw": value, "response_schema": schema}
    if isinstance(value, dict) and ("action_requests" in value or value.get("type") == "tool_approval"):
        actions = value.get("action_requests", [value])
        configs = value.get("review_configs", [])
        request.update(type="approval", protocol="batched" if "action_requests" in value else "per_call", actions=[])
        for position, action in enumerate(actions):
            config = configs[position] if position < len(configs) else {}
            args = action.get("args", {})
            request["actions"].append({
                "name": action.get("name", "工具"), "args": args,
                "command": args.get("command", ""), "description": action.get("description", ""),
                "allowed_decisions": config.get("allowed_decisions") or _allowed(schema) or DEFAULT_DECISIONS,
            })
    elif isinstance(value, dict) and value.get("type") in {"question", "user_question"}:
        request.update(type="question", question=value.get("question", value.get("message", "请补充信息")),
                       options=value.get("options", []))
    elif isinstance(value, dict) and value.get("type") == "approval_request":
        request.update(type="confirmation", question=value.get("message", value.get("action", "请确认操作")))
    else:
        request.update(type="custom", question=(value.get("message", "请提供继续任务所需的信息")
                       if isinstance(value, dict) else str(value)))
    return request


def _args_schema(name):
    if name in {"write_file", "edit_file"}:
        fields = {"file_path": {"type": "string"}}
        required = ["file_path"]
        if name == "write_file":
            fields["content"] = {"type": "string"}
            required.append("content")
        else:
            fields.update(old_string={"type": "string"}, new_string={"type": "string"}, replace_all={"type": "boolean"})
            required += ["old_string", "new_string"]
        return {"type": "object", "properties": fields, "required": required, "additionalProperties": False}
    from tools import get_tools
    tool = next((tool for tool in get_tools("all") if tool.name == name), None)
    if tool is not None:
        schema = tool.tool_call_schema
        return schema.model_json_schema() if hasattr(schema, "model_json_schema") else schema
    return {"type": "object"}


def _validate_schema(value, schema):
    if not schema:
        return
    from jsonschema import Draft202012Validator, ValidationError

    try:
        Draft202012Validator(schema).validate(value)
    except ValidationError as exc:
        raise ValueError(f"回复格式不正确：{exc.message}") from exc


def validate_response(request, response):
    """Return the actual resume value, without executing a tool on invalid input."""
    if request["type"] == "approval":
        if isinstance(response, list):
            decisions = response
        elif isinstance(response, dict) and "decisions" in response:
            decisions = response["decisions"]
        elif request["protocol"] == "per_call":
            decisions = [response]
        else:
            raise ValueError("请为每项操作提供一个审批决策")
        if not isinstance(decisions, list) or len(decisions) != len(request["actions"]):
            raise ValueError("审批决策数量必须与操作数量一致")
        for decision, action in zip(decisions, request["actions"]):
            if not isinstance(decision, dict) or decision.get("type") not in action["allowed_decisions"]:
                raise ValueError(f"{action['name']} 不支持这个决策")
            kind = decision["type"]
            if kind == "edit":
                edited = decision.get("edited_action", {})
                if not isinstance(edited, dict) or edited.get("name") != action["name"]:
                    raise ValueError("修改参数时不能切换工具")
                _validate_schema(edited.get("args"), _args_schema(action["name"]))
                if action["name"] in {"run_bash", "run_powershell"} and not edited["args"]["command"].strip():
                    raise ValueError("修改后的命令不能为空")
            if kind == "respond" and (not isinstance(decision.get("message"), str) or not decision["message"].strip()):
                raise ValueError("回复内容不能为空")
            if "message" in decision and not isinstance(decision["message"], str):
                raise ValueError("拒绝原因或回复必须是文字")
        result = {"decisions": decisions} if request["protocol"] == "batched" else decisions[0]
    elif request["type"] == "question":
        result = {"answer": response} if isinstance(response, str) else response
        if not isinstance(result, dict) or not isinstance(result.get("answer"), str) or not result["answer"].strip():
            raise ValueError("请输入回答或选择一个选项")
    elif request["type"] == "confirmation":
        result = response
        if not isinstance(result, dict) or type(result.get("approved")) is not bool:
            raise ValueError("请选择批准或拒绝")
        if "reason" in result and not isinstance(result["reason"], str):
            raise ValueError("拒绝原因必须是文字")
    else:
        result = response
    _validate_schema(result, request.get("response_schema"))
    json.dumps(result, allow_nan=False)
    return result


def resume_values(items, responses):
    if len(items) == 1 and not getattr(items[0], "id", None):
        return responses[0]
    return {getattr(item, "id", None) or f"interrupt-{index}": value
            for index, (item, value) in enumerate(zip(items, responses))}


def terminal_answer(request, text, index=0):
    """Parse one terminal action; validate the assembled response before resuming."""
    text = text.strip()
    if request["type"] == "question":
        options = request.get("options") or []
        if text.isdecimal() and 0 < int(text) <= len(options):
            text = options[int(text) - 1]
        return {"answer": text}
    if request["type"] == "confirmation":
        if text.lower() in {"y", "yes"}:
            return {"approved": True}
        if text.lower() == "n" or text.lower().startswith("n "):
            return {"approved": False, "reason": text[1:].strip() or "用户拒绝了此操作"}
        raise ValueError("请输入 y 或 n [原因]")
    if request["type"] == "custom":
        try:
            return json.loads(text)
        except ValueError as exc:
            raise ValueError("请以 JSON 格式输入回复") from exc
    action = request["actions"][index]
    if text.lower() in {"y", "yes"}:
        return {"type": "approve"}
    if text.lower() == "n" or text.lower().startswith("n "):
        return {"type": "reject", "message": text[1:].strip() or "用户拒绝执行；不要重复提交同一操作。"}
    if text.lower().startswith("respond "):
        return {"type": "respond", "message": text[8:]}
    try:
        args = {"command": text} if "command" in action["args"] else json.loads(text)
    except ValueError as exc:
        raise ValueError("修改参数请填写完整 JSON 对象") from exc
    return {"type": "edit", "edited_action": {"name": action["name"], "args": args}}


def terminal_prompt(request, index=0):
    if request["type"] == "approval":
        action = request["actions"][index]
        choices = " / ".join(action["allowed_decisions"])
        return (f"[{index + 1}/{len(request['actions'])}] {action['name']}\n"
                f"{action.get('description') or json.dumps(action['args'], ensure_ascii=False)}\n"
                f"允许：{choices}；y=批准，n [原因]=拒绝，或填写修改后的命令/JSON 参数")
    if request["type"] == "question":
        options = "\n".join(f"{i}. {option}" for i, option in enumerate(request.get("options", []), 1))
        return f"{request['question']}\n{options}\n输入选项编号或直接回答"
    if request["type"] == "confirmation":
        return f"{request['question']}\ny=批准 / n [原因]=拒绝"
    return f"{request['question']}\n{json.dumps(request['raw'], ensure_ascii=False)}\n请输入 JSON 回复"


def terminal_review(items, read=None, write=None):
    read, write = read or input, write or print
    responses = []
    for index, item in enumerate(items):
        request = normalize_interrupt(item, index)
        decisions = []
        count = len(request["actions"]) if request["type"] == "approval" else 1
        for position in range(count):
            while True:
                write(terminal_prompt(request, position))
                try:
                    value = terminal_answer(request, read("> "), position)
                    if request["type"] == "approval":
                        action_request = {**request, "actions": [request["actions"][position]], "protocol": "batched", "response_schema": None}
                        validate_response(action_request, [value])
                        decisions.append(value)
                    else:
                        validate_response(request, value)
                    break
                except ValueError as exc:
                    write(str(exc))
        responses.append(validate_response(request, decisions if request["type"] == "approval" else value))
    return resume_values(items, responses)
