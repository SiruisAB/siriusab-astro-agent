"""Run one agent turn with streaming output and explicit approval callbacks."""

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langgraph.types import Command

from main import _msg_reasoning
from hitl import normalize_interrupt, pending_interrupts, resume_values, validate_response
from compact import content_text
from skill_runtime import skill_message


def extract_agent_turn(messages):
    """从一轮 agent 结果提取 (最终回答, 思考过程 dict)。

    思考过程来源：本轮各 AIMessage 的 reasoning_content + 工具调用轨迹
    （tool_calls 与对应 ToolMessage 的配对摘要）。
    """
    turn = []
    for msg in reversed(messages):
        if isinstance(msg, HumanMessage):
            break
        turn.append(msg)
    turn.reverse()

    trace = []
    reasoning_parts = []
    for i, msg in enumerate(turn):
        r = _msg_reasoning(msg)
        if r:
            reasoning_parts.append(r)
        for tc in getattr(msg, "tool_calls", None) or []:
            result = ""
            for later in turn[i + 1:]:
                if isinstance(later, ToolMessage) and later.tool_call_id == tc["id"]:
                    result = str(later.content)[:150]
                    break
            trace.append({
                "tool": tc.get("name", "?"),
                "args": {k: str(v)[:80] for k, v in (tc.get("args") or {}).items()},
                "result": result,
            })
    answer = content_text(turn[-1].content) if turn and isinstance(turn[-1], AIMessage) else ""
    return answer, {"draft": None, "trace": trace,
                    "reasoning": "\n\n".join(reasoning_parts)}


def interaction_answers(messages):
    """Keep actual user answers available in the next task's conversation context."""
    answers = []
    for index, message in enumerate(messages):
        for call in getattr(message, "tool_calls", None) or []:
            if call.get("name") != "ask_user":
                continue
            result = next((item for item in messages[index + 1:] if isinstance(item, ToolMessage)
                           and item.tool_call_id == call["id"] and item.status != "error"), None)
            if result is not None:
                answers.append(HumanMessage(content=f"[用户回答]\n{call['args'].get('question', '')}：{result.content}",
                                             additional_kwargs={"interaction_answer": True}))
    return answers


def run_agent_turn(agent, config, messages, *, on_answer, on_approval=None, on_interrupts=None, on_tool=None, turn_control=None):
    def review(items):
        if on_interrupts is not None:
            return Command(resume=on_interrupts(items))
        values = []
        for index, item in enumerate(items):
            value = on_approval(item.value)
            values.append(validate_response(normalize_interrupt(item, index), value))
        return Command(resume=resume_values(items, values))

    existing = pending_interrupts(agent.get_state(config)) if messages is None else []
    if messages is None and not existing:
        raise ValueError("当前没有等待处理的中断，不能自动重跑工具。")
    if messages is not None and pending_interrupts(agent.get_state(config)):
        raise ValueError("当前任务正在等待处理，请先使用 /resume 恢复。")
    if messages and config.get("configurable", {}).get("skill_context"):
        messages = [*messages[:-1], skill_message(config["configurable"]["skill_context"]), messages[-1]]
    inputs = review(existing) if existing else {"messages": messages}
    while True:
        pending = {}
        for kind, data in agent.stream(inputs, config=config, stream_mode=["messages", "updates"]):
            if turn_control:
                turn_control.check()
            if kind == "messages":
                chunk, metadata = data
                if "restricted-subagent" in metadata.get("tags", []):
                    continue
                if isinstance(chunk, AIMessageChunk) and (text := content_text(chunk.content)):
                    on_answer(text)
            elif kind == "updates":
                if "__interrupt__" in data:
                    for index, item in enumerate(data["__interrupt__"]):
                        pending[getattr(item, "id", None) or f"interrupt-{index}"] = item
                if on_tool is not None:
                    for update in data.values():
                        if isinstance(update, dict):
                            for message in update.get("messages", []):
                                if isinstance(message, ToolMessage):
                                    on_tool(message)
        if not pending:
            if turn_control and not turn_control.seal_if_empty():
                inputs = {"messages": turn_control.additions()}
                continue
            break
        inputs = review(list(pending.values()))
    state = agent.get_state(config)
    return extract_agent_turn(state.values.get("messages", []))
