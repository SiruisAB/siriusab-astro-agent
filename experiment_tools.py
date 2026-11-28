"""Scoped metadata tools; execution remains with existing approved Shell tools."""

import json

from experiment_store import ExperimentStore, digest

RULES = (
    "\n科研实验：create_experiment 仅冻结显式源码、输入校验值、Git 和环境版本，不执行命令。"
    "保存运行前基线优先于直接生成更多代码；参数、种子、单位和数据来源应明确。"
    "list_experiments/read_experiment/check_reproduction 只查询当前会话或用户显式共享的记录。"
    "结果由用户在实验面板登记，不能伪造程序已经运行、退出码或结果。"
    "条件匹配不是科学复现成功；数值证据、模型推导和机器验证证明必须区分。"
    "实际代码运行、依赖安装、远程提交仍使用原工具与审批；不要用实验登记绕过权限。"
)


def agent_tools(root, mode, on_event=None):
    from langchain.tools import ToolRuntime, tool
    from langchain_core.messages import AIMessage
    store = ExperimentStore(root)

    def owner(runtime):
        cfg = runtime.config.get("configurable", {})
        return cfg.get("conversation_id") or cfg.get("thread_id", ""), cfg

    @tool
    def list_experiments(runtime: ToolRuntime) -> str:
        """列出当前会话与显式共享的科研实验计划及人工登记状态，不执行实验。"""
        sid, _ = owner(runtime)
        return json.dumps(store.list(mode, sid), ensure_ascii=False)

    @tool
    def read_experiment(experiment_id: str, runtime: ToolRuntime) -> str:
        """读取科研实验的基线与结果摘要。不返回源码全文、密钥或整个 Python 环境列表。人工结果未独立验证。"""
        sid, _ = owner(runtime)
        r = store.get(experiment_id, mode, sid)
        b = r["baseline"]
        return json.dumps({"id":r["id"],"revision":r["revision"],"status":r["status"],"spec":r["spec"],"code":b["code"],"inputs":b["inputs"],
            "git":{k:v for k,v in b["git"].items() if k != "selected_diff"},
            "environment":{k:v for k,v in b["environment"].items() if k not in {"packages","executable"}},
            "outcome":r["outcome"],"warnings":b["warnings"],"execution_observed_by_workbench":False},ensure_ascii=False)

    @tool
    def check_reproduction(experiment_id: str, runtime: ToolRuntime) -> str:
        """核对当前显式源码、输入、Git 与环境版本是否符合冻结基线。不运行命令，不修改环境，不证明科学结论。"""
        sid, _ = owner(runtime)
        return json.dumps(store.check(experiment_id, mode, sid), ensure_ascii=False)

    @tool
    def create_experiment(title: str, hypothesis: str, command: str, code_files: list[str], runtime: ToolRuntime,
                          input_files: list[str] = [], parameters: dict = {}, seed: int | None = None,
                          units: dict = {}, hardware_notes: str = "") -> str:
        """建立科研实验计划，冻结最多16个项目相对路径源码及输入校验值，记录命令、参数、种子和单位。只是登记，不执行命令、不安装依赖。"""
        sid, cfg = owner(runtime)
        messages = runtime.state.get("messages", [])
        parent = next((message.id for message in reversed(messages) if isinstance(message, AIMessage)
                       and any(call["id"] == runtime.tool_call_id for call in message.tool_calls)), len(messages))
        key = digest([mode,sid,cfg.get("run_id") or cfg.get("thread_id"),parent,runtime.tool_call_id])
        result = store.create({"title":title,"hypothesis":hypothesis,"command":command,"code_files":code_files,"input_files":input_files,
                              "parameters":parameters,"seed":seed,"units":units,"hardware_notes":hardware_notes}, mode, sid, operation_key=key)
        if on_event: on_event({"id":result["id"],"status":result["status"],"title":result["spec"]["title"],"command_executed":False})
        return json.dumps({"id":result["id"],"status":result["status"],"revision":result["revision"],"command_executed":False,
                           "warnings":result["baseline"]["warnings"],"message":"已冻结基线；实际运行须另经原执行工具审批，结果由用户在实验面板登记"}, ensure_ascii=False)

    tools = [list_experiments, read_experiment, check_reproduction]
    return tools + ([create_experiment] if mode in {"agent","code","research"} else [])
