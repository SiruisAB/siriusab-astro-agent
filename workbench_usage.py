"""Report token usage for the latest model call, including calls that use tools."""

from time import perf_counter

from langchain_core.callbacks import BaseCallbackHandler
from langchain_openai import ChatOpenAI


def _token_count(value):
    return value if type(value) is int and value >= 0 else None


def _normalize_usage(chunk):
    """DeepSeek may report its cache count outside prompt_tokens_details."""
    usage = chunk.get("usage") or {}
    cached = _token_count(usage.get("prompt_cache_hit_tokens"))
    details = usage.get("prompt_tokens_details") or {}
    if cached is None or details.get("cached_tokens") is not None:
        return chunk
    return {**chunk, "usage": {**usage, "prompt_tokens_details": {**details, "cached_tokens": cached}}}


class WorkbenchModel(ChatOpenAI):
    def _convert_chunk_to_generation_chunk(self, chunk, default_chunk_class, base_generation_info):
        return super()._convert_chunk_to_generation_chunk(
            _normalize_usage(chunk), default_chunk_class, base_generation_info,
        )

    def _create_chat_result(self, response, generation_info=None):
        result = super()._create_chat_result(response, generation_info)
        raw_usage = (result.llm_output or {}).get("token_usage") or {}
        cached = _token_count(raw_usage.get("prompt_cache_hit_tokens"))
        if cached is not None:
            for generation in result.generations:
                usage = generation.message.usage_metadata
                if usage is not None:
                    usage.setdefault("input_token_details", {}).setdefault("cache_read", cached)
        return result


class ModelUsage(BaseCallbackHandler):
    def __init__(self, publish, context_window=None):
        self.publish = publish
        self.context_window = context_window
        self.started = {}

    def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
        self.started[run_id] = perf_counter()

    def on_llm_end(self, response, *, run_id, **kwargs):
        started = self.started.pop(run_id, None)
        if started is None:
            return
        elapsed = perf_counter() - started
        generation = next((group[0] for group in response.generations if group), None)
        usage = getattr(getattr(generation, "message", None), "usage_metadata", None) or {}
        output = _token_count(usage.get("output_tokens"))
        details = usage.get("input_token_details") or {}
        cached = next((details[key] for key in ("cache_read", "priority_cache_read", "flex_cache_read")
                       if _token_count(details.get(key)) is not None), None)
        self.publish({
            "input": _token_count(usage.get("input_tokens")),
            "output": output,
            "rate": round(output / elapsed, 1) if output is not None and elapsed > 0 else None,
            "cached": cached,
            "limit": self.context_window,
        })

    def on_llm_error(self, error, *, run_id, **kwargs):
        self.started.pop(run_id, None)
