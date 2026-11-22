import asyncio
import json
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage

import main
import workspace
from chatstore import ChatStore


PROFILES = {"test": {"model": "offline", "api_key": "test-only"}}


def connect(client):
    return client.ws_connect("/api/run", origin=str(client.make_url("/")).rstrip("/"))


def test_workbench_chat_reads_legacy_history_and_saves_streamed_turn(tmp_path, monkeypatch):
    store = ChatStore(tmp_path / "sessions.db")
    sid = store.create_session("观测记录")
    store.replace_messages(sid, [HumanMessage(content="之前的问题"), AIMessage(content="**旧回答**")])
    (tmp_path / "stars.csv").write_text("name,mag\nM31,3.4", encoding="utf-8")
    (tmp_path / "plot.py").write_text('def plot():\n    return "<script>"\n', encoding="utf-8")
    (tmp_path / ".env").write_text("PRIVATE=example", encoding="utf-8")
    (tmp_path / "large.txt").write_bytes(b"a" * (workspace.PREVIEW_BYTES + 1))
    monkeypatch.setattr(workspace, "build_model", lambda _, **options: object())

    def chat_reply(model, history, question, **kwargs):
        assert [message.content for message in history] == ["之前的问题", "**旧回答**"]
        assert kwargs["attachments"] == [str(tmp_path / "stars.csv")]
        kwargs["on_draft"]("草稿")
        kwargs["on_answer"]("观")
        kwargs["on_answer"]("测")
        answer = "# 观测\n\n<script>alert(1)</script>\n\n```python\nreturn M31\n```"
        history.extend([HumanMessage(content=question), AIMessage(content=answer)])
        return answer, {"draft": "草稿"}

    monkeypatch.setattr(main, "chat_reply", chat_reply)

    async def run():
        async with TestClient(TestServer(workspace.create_app(tmp_path, PROFILES))) as client:
            async with client.get("/api/state") as response:
                assert response.status == 200
                state = await response.json()
                assert state["sessions"][0]["id"] == sid
                assert state["profiles"] == [{"id": "test", "model": "offline"}]
            async with client.get(f"/api/sessions/{sid}") as response:
                history = await response.json()
                assert "<strong>旧回答</strong>" in history[-1]["html"]
            async with client.get("/api/files") as response:
                assert [item["name"] for item in await response.json()] == ["large.txt", "plot.py", "stars.csv"]
            async with client.get("/api/preview", params={"path": "plot.py"}) as response:
                preview = await response.json()
                assert '<span class="syn-k">def</span>' in preview["html"]
                assert "<script>" not in preview["html"]
            async with client.get("/api/preview", params={"path": ".env"}) as response:
                assert response.status == 404
            async with client.get("/api/preview", params={"path": "large.txt"}) as response:
                info = await response.json()
                assert info["truncated"] and len(info["text"]) == workspace.PREVIEW_BYTES
            async with connect(client) as socket:
                await socket.send_json({"mode": "chat", "profile": "test", "session": sid,
                                        "text": "新问题", "attachments": ["stars.csv"]})
                events = [await socket.receive_json(timeout=3) for _ in range(4)]
                assert [event["kind"] for event in events] == ["session", "draft", "delta", "done"]
                assert events[2]["text"] == "观测"
                assert "<h1>观测</h1>" in events[3]["html"]
                assert "<script>" not in events[3]["html"]
                assert '<span class="syn-k">return</span>' in events[3]["html"]
            assert len(store.load_messages(sid)) == 4
            async with client.get("/api/state", params={"mode": "agent"}) as response:
                assert (await response.json())["sessions"] == []

    asyncio.run(run())


def test_workbench_task_waits_for_approval_and_restores_its_own_history(tmp_path, monkeypatch):
    resumed = []
    agent_history = []
    final_answer = "执行结束"

    class FakeAgent:
        def stream(self, inputs, *, config, stream_mode):
            if isinstance(inputs, dict):
                agent_history[:] = inputs["messages"]
                yield "messages", (AIMessageChunk(content="准备"), {})
                yield "updates", {"__interrupt__": [SimpleNamespace(value={"action_requests": [
                    {"name": "run_powershell", "args": {"command": "Get-Date"}},
                ]})]}
            else:
                resumed.extend(inputs.resume["decisions"])
                yield "updates", {"tools": {"messages": [ToolMessage(
                    content="未执行", name="run_powershell", tool_call_id="call_1",
                )]}}

        def get_state(self, config):
            return SimpleNamespace(values={"messages": [
                *agent_history, AIMessage(content=final_answer),
            ]})

    monkeypatch.setattr(workspace, "build_model", lambda _, **options: object())
    monkeypatch.setattr(workspace, "build_agent", lambda *args, **kwargs: (FakeAgent(), {"configurable": {}}))

    async def run():
        async with TestClient(TestServer(workspace.create_app(tmp_path, PROFILES))) as client:
            async with client.get("/api/run", headers={"Origin": "https://example.com"}) as response:
                assert response.status == 403
            async with connect(client) as socket:
                await socket.send_json({"mode": "code", "profile": "test", "text": "显示时间"})
                sid = (await socket.receive_json(timeout=3))["id"]
                assert (await socket.receive_json(timeout=3))["text"] == "准备"
                approval = await socket.receive_json(timeout=3)
                assert approval["actions"][0]["command"] == "Get-Date"
                assert resumed == []
                await socket.send_json({"kind": "decision", "id": "stale", "choice": "approve"})
                await socket.send_json({"kind": "decision", "id": approval["id"], "choice": "reject"})
                assert (await socket.receive_json(timeout=3))["kind"] == "tool"
                assert (await socket.receive_json(timeout=3))["kind"] == "done"
                assert [item["type"] for item in resumed] == ["reject"]
            async with client.get(f"/api/sessions/{sid}", params={"mode": "code"}) as response:
                messages = await response.json()
                assert [message["role"] for message in messages] == ["user", "assistant"]
                assert final_answer in messages[-1]["html"]
            async with client.get(f"/api/sessions/{sid}", params={"mode": "chat"}) as response:
                assert response.status == 404

    asyncio.run(run())


def test_workbench_usage_is_per_model_call_and_preserves_missing_cache(monkeypatch):
    import workbench_usage
    from langchain_core.outputs import ChatGeneration, LLMResult

    event_times = iter([1, 3, 4, 5, 6, 7])
    monkeypatch.setattr(workbench_usage, "perf_counter", lambda: next(event_times))
    reports = []
    tracker = workbench_usage.ModelUsage(reports.append, context_window=1000)

    def report(run_id, usage):
        tracker.on_chat_model_start({}, [], run_id=run_id)
        tracker.on_llm_end(LLMResult(generations=[[ChatGeneration(
            message=AIMessage(content="answer", usage_metadata=usage),
        )]]), run_id=run_id)

    report("first", {"input_tokens": 200, "output_tokens": 40, "total_tokens": 240,
                     "input_token_details": {"cache_read": 80}})
    assert reports[-1] == {"input": 200, "output": 40, "rate": 20.0, "cached": 80, "limit": 1000}
    report("second", {"input_tokens": 400, "output_tokens": 100, "total_tokens": 500,
                      "input_token_details": {"cache_read": 0}})
    assert reports[-1] == {"input": 400, "output": 100, "rate": 100.0, "cached": 0, "limit": 1000}
    report("third", None)
    assert reports[-1] == {"input": None, "output": None, "rate": None, "cached": None, "limit": 1000}


def test_stream_usage_from_openai_compatible_endpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "get_tools", lambda _: [])
    monkeypatch.setenv("CHAT_TWO_STAGE", "0")
    monkeypatch.setenv("CONTEXT_WINDOW_TEST", "1000")
    monkeypatch.setenv("STREAM_USAGE_TEST", "1")

    async def completion(request):
        payload = await request.json()
        assert payload["stream_options"] == {"include_usage": True}
        assert payload["temperature"] == 0.3
        meta = {"id": "local", "object": "chat.completion.chunk", "created": 1, "model": "offline"}
        chunks = [
            {**meta, "choices": [{"index": 0, "delta": {"role": "assistant", "content": "Hello"}}]},
            {**meta, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            {**meta, "choices": [], "usage": {"prompt_tokens": 200, "completion_tokens": 20,
                                             "total_tokens": 220, "prompt_cache_hit_tokens": 100}},
        ]
        stream = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
        return web.Response(text=stream, content_type="text/event-stream")

    async def run():
        upstream = web.Application()
        upstream.router.add_post("/v1/chat/completions", completion)
        async with TestServer(upstream) as model_server:
            profiles = {"test": {**PROFILES["test"], "base_url": str(model_server.make_url("/v1"))}}
            async with TestClient(TestServer(workspace.create_app(tmp_path, profiles))) as client:
                store = ChatStore(tmp_path / "sessions.db")
                sid = store.create_session()
                store.set_params(sid, {"temperature": 0.3})
                async with connect(client) as socket:
                    await socket.send_json({"mode": "chat", "profile": "test", "text": "Hello", "session": sid})
                    events = [await socket.receive_json(timeout=5) for _ in range(4)]
                    assert [event["kind"] for event in events] == ["session", "delta", "usage", "done"], events
                    usage = events[2]
                    assert usage["rate"] > 0
                    assert {key: usage[key] for key in ("input", "output", "cached", "limit")} == {
                        "input": 200, "output": 20, "cached": 100, "limit": 1000,
                    }

    asyncio.run(run())
