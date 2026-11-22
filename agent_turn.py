"""Run one agent turn with streaming output and explicit approval callbacks."""

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langgraph.types import Command

from main import _msg_reasoning


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
    answer = turn[-1].content if turn and isinstance(turn[-1], AIMessage) else ""
    return answer, {"draft": None, "trace": trace,
                    "reasoning": "\n\n".join(reasoning_parts)}


def run_agent_turn(agent, config, messages, *, on_answer, on_approval, on_tool=None):
    inputs = {"messages": messages}
    while True:
        pending = None
        for kind, data in agent.stream(inputs, config=config, stream_mode=["messages", "updates"]):
            if kind == "messages":
                chunk, _ = data
                if isinstance(chunk, AIMessageChunk) and isinstance(chunk.content, str) and chunk.content:
                    on_answer(chunk.content)
            elif kind == "updates":
                if "__interrupt__" in data:
                    pending = data["__interrupt__"]
                if on_tool is not None:
                    for update in data.values():
                        if isinstance(update, dict):
                            for message in update.get("messages", []):
                                if isinstance(message, ToolMessage):
                                    on_tool(message)
        if not pending:
            break
        decisions = on_approval(pending[0].value)
        inputs = Command(resume={"decisions": decisions})
    state = agent.get_state(config)
    return extract_agent_turn(state.values.get("messages", []))
