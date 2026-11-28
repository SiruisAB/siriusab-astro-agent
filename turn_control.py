"""Per-turn stop signals and user supplements, without global model limits."""

import contextvars
import threading
from contextlib import contextmanager, suppress

CURRENT = contextvars.ContextVar("workbench_turn_control", default=None)


class TurnStopped(BaseException):
    """Bypass automatic model/tool retries after an explicit user stop."""


class TurnControl:
    def __init__(self, publish=None):
        self.stopped = threading.Event()
        self.lock = threading.RLock()
        self.pending = []
        self.applied = []
        self.seen = {}
        self.accepting = True
        self.responses = []
        self.clients = set()
        self.publish = publish or (lambda *args, **kwargs: None)

    @contextmanager
    def activate(self):
        token = CURRENT.set(self)
        try:
            yield
        finally:
            CURRENT.reset(token)

    def check(self):
        if self.stopped.is_set():
            raise TurnStopped()

    def enqueue(self, identifier, text):
        with self.lock:
            if identifier in self.seen:
                if self.seen[identifier] != text:
                    raise ValueError("补充消息标识已使用，不能改写内容")
                return False
            if not self.accepting or self.stopped.is_set():
                raise ValueError("本轮已结束或正在停止，请在结束后发送补充消息")
            if len(self.seen) >= 20 or sum(len(value) for value in self.seen.values()) + len(text) > 48000:
                raise ValueError("本轮补充消息最多 20 条、合计 48000 字符")
            self.seen[identifier] = text
            self.pending.append({"id": identifier, "text": text})
        return True

    def has_pending(self):
        with self.lock:
            return bool(self.pending)

    def additions(self):
        from langchain_core.messages import HumanMessage
        self.check()
        with self.lock:
            items, self.pending = self.pending, []
            self.applied.extend(items)
        for item in items:
            self.publish("steer_applied", id=item["id"], text=item["text"])
        return [HumanMessage(content=item["text"], id="steer-" + item["id"],
                             additional_kwargs={"user_supplement": True, "supplement_id": item["id"]}) for item in items]

    def applied_messages(self, start=0):
        from langchain_core.messages import HumanMessage
        with self.lock:
            return [HumanMessage(content=item["text"], id="steer-" + item["id"],
                                 additional_kwargs={"user_supplement": True, "supplement_id": item["id"]}) for item in self.applied[start:]]

    def seal_if_empty(self):
        self.check()
        with self.lock:
            if self.pending:
                return False
            self.accepting = False
            return True

    def stop(self):
        with self.lock:
            if self.stopped.is_set():
                return
            self.stopped.set()
            self.accepting = False
            responses = list(self.responses)
        for response in responses:
            with suppress(Exception):
                response.close()

    def track_model(self, model):
        # Each GUI turn owns these SDK clients; never close a shared global client.
        client = getattr(getattr(model, "root_client", None), "_client", None)
        if client is None or not hasattr(client, "event_hooks") or id(client) in self.clients:
            return model
        self.clients.add(id(client))
        def request_hook(request):
            self.check()
        def response_hook(response):
            with self.lock:
                self.responses.append(response)
            if self.stopped.is_set():
                response.close()
                self.check()
        client.event_hooks.setdefault("request", []).append(request_hook)
        client.event_hooks.setdefault("response", []).append(response_hook)
        return model


def check_turn():
    control = CURRENT.get()
    if control:
        control.check()


def control_middleware(control):
    """Keep new messages after paired tool results; do not run stale tool plans."""
    from langchain.agents.middleware import AgentMiddleware
    from langchain_core.messages import ToolMessage
    class LiveControls(AgentMiddleware):
        def before_model(self, state, runtime):
            additions = control.additions()
            return {"messages": additions} if additions else None

        def wrap_model_call(self, request, handler):
            control.check()
            result = handler(request)
            control.check()
            return result

        def wrap_tool_call(self, request, handler):
            control.check()
            if control.has_pending():
                return ToolMessage(content="用户已补充新方向，本次旧工具请求已取消，不执行或重跑；已完成操作以持久记录为准。请读取新指令重新规划。",
                                   tool_call_id=request.tool_call["id"], name=request.tool_call["name"], status="error")
            return handler(request)
    return LiveControls()
