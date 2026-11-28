"""Archive model inputs and untruncated tool results inside the Python memory store."""

import asyncio

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import BaseMessage
from langgraph.config import get_config
from langgraph.types import Command

from chatstore import ChatStore


class ConversationArchive(AgentMiddleware):
    def _archive(self, messages, kind, summary=""):
        cfg = get_config().get("configurable", {})
        if not messages or not cfg.get("memory_db") or not cfg.get("conversation_id"):
            return
        store = ChatStore(cfg["memory_db"])
        store.archive_messages(cfg["conversation_id"], messages, run_id=cfg.get("run_id", ""),
                               thread_id=cfg.get("thread_id", ""), kind=kind, summary=summary)

    def _input(self, request):
        self._archive(request.state.get("messages", []), "trace")
        event = request.state.get("_summarization_event")
        if event and event.get("summary_message"):
            message = event["summary_message"]
            self._archive([message], "automatic_summary", str(message.content))

    def _result(self, result):
        messages = result.update.get("messages", []) if isinstance(result, Command) and isinstance(result.update, dict) else result
        if isinstance(messages, BaseMessage):
            messages = [messages]
        if isinstance(messages, list):
            self._archive([message for message in messages if isinstance(message, BaseMessage)], "tool_result")

    def wrap_model_call(self, request, handler):
        self._input(request)
        return handler(request)

    async def awrap_model_call(self, request, handler):
        await asyncio.to_thread(self._input, request)
        return await handler(request)

    def wrap_tool_call(self, request, handler):
        result = handler(request)
        self._result(result)
        return result

    async def awrap_tool_call(self, request, handler):
        result = await handler(request)
        await asyncio.to_thread(self._result, result)
        return result
