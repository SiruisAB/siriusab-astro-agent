"""天文 Agent 主程序。

四种模式（运行中用 /mode 切换，或启动时 --mode <名称>，--chat 等价于 --mode chat）：

- agent    通用模式：查天体数据、算坐标、执行 shell（需审批），工具循环
- research 科研构思：提出有新意的科学 idea、设计验证路径（仅查询类工具）
- code     代码工程：读代码、改代码、运行验证（含 shell，需审批）
- chat     学术问答：两阶段深度思考后一次性输出，可查 arXiv/ADS/SIMBAD

每个模式是独立的 agent 实例：各自的提示词、工具子集、对话记忆互不共享。

模型支持多档案热切换：.env 中用 MODEL_NAME_<名字> 定义附加档案，运行中 /model <名字>
切换（或启动时 --model <名字>）。对话历史存在 sessions.db，与模型无关——新模型读取
完整历史即可无缝衔接此前进展。

运行前：复制 .env.example 为 .env 并填入你的 API key。
"""

import os
import sqlite3
import sys

from dotenv import load_dotenv
from deepagents.backends import FilesystemBackend
from deepagents.middleware import FilesystemMiddleware
from langchain.agents import create_agent
from langchain.agents.middleware import (
    HumanInTheLoopMiddleware,
    ModelRetryMiddleware,
    SummarizationMiddleware,
    TodoListMiddleware,
    ToolRetryMiddleware,
)
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from tools import get_tools
from viewer import list_tree, parse_range, view_file

# ========== 各模式的系统提示词 ==========

SYSTEM_PROMPT = (
    "你是一个严谨的天文助手。回答天体数据问题时，必须先调用工具查询，"
    "不要凭记忆编造坐标、星等、距离等数值。"
    "用户用中文提问时用中文回答；天体名称传给工具时用英文名或星表编号"
    "（如织女星用 Vega，仙女座星系用 M31）。"
    "查询结果要做简单解读，而不只是罗列数字。\n"
    "讨论文献时可调用 search_arxiv / search_ads 检索论文，"
    "引用时给出 arXiv ID 或 bibcode。\n"
    "你拥有文件工具（read_file/write_file/edit_file 等），可以读写项目目录内的文件；"
    "大的查询结果会自动存入 results/ 目录并以文件路径引用。\n"
    "当对话变得很长、早期内容不再需要逐字保留时，调用 compress 压缩旧历史"
    "（摘要必须保留数值结果、文件路径、关键决策），需要细节时 decompress 恢复。\n"
    "你还可以调用 run_powershell / run_bash 执行 shell 命令，用于文件操作、"
    "运行脚本、调用本地工具等。注意：每条 shell 命令执行前用户都会被要求确认，"
    "若用户拒绝，请改用其他方式或直接询问用户意图；不要重复提交同一条被拒绝的命令。"
)

RESEARCH_PROMPT = (
    "你是一位资深的天体物理科研合作者，与用户平等讨论科研问题。\n"
    "你的核心价值是提出真正有新意的科学想法，而不是复述教科书结论。"
    "你配有文献检索工具（search_arxiv 查 preprint、search_ads 查正式期刊论文）"
    "和天体数据查询工具（SIMBAD）：\n"
    "- 查新优先：提出或讨论任何新想法前，先用 search_arxiv / search_ads "
    "多角度检索相关工作（关键词、物理机制、天体类别分别检索），"
    "确认新意点确实未被做过；已有相近工作时明确指出与它们的差异，"
    "或诚实地建议调整方向\n"
    "- 想法必须给出五要素：科学问题、核心创新点（明确说清与已有工作的区别）、"
    "为什么现在可行（数据/方法/观测设施的窗口期）、可执行的验证路径、主要风险与替代方案\n"
    "- 鼓励大胆的跨领域组合和未被检验的假设，但用物理合理性把关：能量量级、时标、"
    "统计陷阱（选择效应、参数简并、后验偏差）；发现用户想法的漏洞要直说并给出修正\n"
    "- 涉及具体天体参数（坐标、距离、星等）时调用查询工具核实，不得编造数值\n"
    "- 引用文献时给出准确的 arXiv ID 或 bibcode 与发表年份；"
    "检索不到相关文献时明确说明'未检索到，此说法待核实'，绝不臆造文献\n"
    "用中文讨论，专业术语保留英文。"
)

CODE_PROMPT = (
    "你是一名严谨的软件工程师，直接动手完成任务而不是纸上谈兵。\n"
    "工作方式：\n"
    "- 优先使用文件工具（read_file/write_file/edit_file/glob）查看和修改代码，"
    "edit_file 做精确文本替换；shell 仅用于运行命令和文件工具做不到的操作\n"
    "- 修改前先读现有代码，遵循项目现有的风格与命名习惯\n"
    "- 做最小必要改动；改完主动运行命令验证（编译/测试/执行），"
    "用真实输出说明结果，失败就修到通过\n"
    "- 删除、覆盖、批量修改等危险操作会先经用户审批；被拒绝后不要重试同一条命令\n"
    "- 需求有歧义时先问清再动手；完成后简述改了什么、如何验证\n"
    "用中文交流，代码与注释遵循项目现有语言习惯。"
)

# chat 模式：两阶段生成（均可自主调用文献/数据检索工具）。
CHAT_DRAFT_PROMPT = (
    "你是一位天体物理学者，正在为一次正式回答撰写内部草稿。草稿不直接展示给用户，"
    "请自由、深入地推理，不要担心结构：\n"
    "- 列出问题涉及的所有物理机制、相关定律与典型量级\n"
    "- 做数量级估算（Fermi 估算）检验每一种说法是否站得住\n"
    "- 主动寻找反例、边界条件和大众常见误解\n"
    "- 需要文献支撑或最新进展时，调用 search_arxiv / search_ads 检索；"
    "涉及具体天体参数时调用 SIMBAD 查询工具核实\n"
    "- 明确标注哪些是已确立结论、哪些处于学术争议、哪些你自己不确定\n"
    "直接输出草稿内容，不要寒暄。"
)

CHAT_PROMPT = (
    "你是一位理论功底扎实的天体物理学者。你可以调用检索工具核实信息："
    "search_arxiv / search_ads 查文献（引用时给出 arXiv ID 或 bibcode），"
    "SIMBAD 查询工具核实天体参数。用户消息中可能附带一份内部思考草稿，"
    "它是原始分析材料：请核查并修正其中的错误，取其精华，输出更严谨的最终回答。\n"
    "回答结构：\n"
    "1. 结论先行：一两句话直接回答核心问题\n"
    "2. 理论依据：引用具体的物理定律、公式或量级估算支撑判断"
    "（如维恩位移定律、Stefan-Boltzmann 定律、维里定理、哈勃定律），"
    "给出推理链条，关键判断配 Fermi 估算；引用文献时给出来源标识\n"
    "3. 全面性：涵盖主要影响因素、边界条件与不同情形下的差异；"
    "存在学术争议时列出各方观点与证据强度\n"
    "4. 准确性：明确区分'已确立的结论'、'活跃争议'与'个人推断'；"
    "不确定处标注置信度（高/中/低）及原因，绝不编造精确数值；"
    "工具检索不到的信息如实说明，不臆造文献\n"
    "5. 无法用工具核实的信息，在末尾建议用户 /mode research 深入讨论\n"
    "用中文回答，专业术语保留英文。"
)

# ========== 模式配置：提示词 + 工具子集 ==========

MODE_CONFIGS = {
    "agent": {
        "desc": "通用模式：查天体数据、算坐标、执行 shell（需审批）",
        "prompt": SYSTEM_PROMPT,
        "tools": "all",
    },
    "code": {
        "desc": "代码工程：读代码、改代码、运行验证（含 shell，需审批）",
        "prompt": CODE_PROMPT,
        "tools": "all",
    },
    "chat": {
        "desc": "学术问答（两阶段深度思考，可查 arXiv/ADS/SIMBAD，无 shell）",
        "prompt": CHAT_PROMPT,
        "tools": "query",
    },
    "research": {
        "desc": "科研构思：查新优先（提 idea 前先检索 arXiv/ADS 确认新颖性），五要素结构化",
        "prompt": RESEARCH_PROMPT,
        "tools": "research",
    },
}

HELP_TEXT = """
命令：
  /mode [agent|research|code|chat]  切换模式
  /mode                             查看所有模式与当前选择
  /model [档案名]                   切换底层模型（档案在 .env 用 MODEL_NAME_<名字> 配置）
  /model                            查看已配置的模型档案与当前模型
  /view <文件> [起始|起始-结束]     查看文件：md 渲染 / 代码高亮+行号 / csv 表格 / json
  /tree [深度]                      树形查看项目目录（默认 2 层）
  /browse                           双栏文件浏览器（左目录树 / 右预览，q 退回对话）
  /prompt <内容>                    为当前模式设置附加提示词（可有可无，叠加在默认提示词上）
  /prompt off                       清除当前模式的附加提示词
  /prompt                           查看当前模式的附加提示词
  /help                             显示本帮助
  q                                 退出
"""


def compose_system(default_prompt: str, custom_prompt: str) -> str:
    """组装系统提示词：默认提示词 + 可选的用户附加要求。"""
    if custom_prompt:
        return f"{default_prompt}\n\n[用户附加要求]\n{custom_prompt}"
    return default_prompt


def list_model_profiles() -> dict:
    """扫描 .env 中配置的全部模型档案，返回 档案名 -> {model, api_key, base_url}。

    约定：主档案无后缀（MODEL_NAME / API_KEY / BASE_URL），档案名固定 default；
    附加档案加任意后缀，如 MODEL_NAME_DEEPSEEK / API_KEY_DEEPSEEK / BASE_URL_DEEPSEEK，
    档案名即后缀的小写（deepseek）。API_KEY_<名字> 缺省时沿用主档案的 API_KEY。
    """
    load_dotenv()
    profiles: dict = {}
    for raw_key, value in os.environ.items():
        key = raw_key.upper()
        if not value:
            continue
        if key == "MODEL_NAME":
            profiles["default"] = {
                "model": value,
                "api_key": os.environ.get("API_KEY"),
                "base_url": os.environ.get("BASE_URL") or None,
            }
        elif key.startswith("MODEL_NAME_"):
            name = key[len("MODEL_NAME_"):].lower()
            profiles[name] = {
                "model": value,
                "api_key": os.environ.get(f"API_KEY_{name.upper()}") or os.environ.get("API_KEY"),
                "base_url": os.environ.get(f"BASE_URL_{name.upper()}") or None,
            }
    # 只填了 API_KEY 没填 MODEL_NAME 时，主档案用默认模型名兜底
    if "default" not in profiles and os.environ.get("API_KEY"):
        profiles["default"] = {
            "model": os.environ.get("MODEL_NAME", "gpt-4o-mini"),
            "api_key": os.environ.get("API_KEY"),
            "base_url": os.environ.get("BASE_URL") or None,
        }
    return profiles


def build_model(profile: dict | None = None) -> ChatOpenAI:
    """构建模型实例（OpenAI 兼容协议）。profile 缺省时读 .env 主档案。"""
    load_dotenv()
    if profile is None:
        profile = {
            "model": os.environ.get("MODEL_NAME", "gpt-4o-mini"),
            "api_key": os.environ.get("API_KEY"),
            "base_url": os.environ.get("BASE_URL") or None,
        }
    if not profile.get("api_key"):
        raise ValueError(
            f"模型档案 {profile.get('model', '?')} 缺少 API_KEY"
            "（检查 .env 中的 API_KEY 或 API_KEY_<档案名>）"
        )
    return ChatOpenAI(
        model=profile["model"],
        api_key=profile["api_key"],
        base_url=profile.get("base_url") or None,  # OpenAI 官方可不填
        temperature=0,  # 天文数据要准确，不发挥
    )


_CHECKPOINTER = None


def _get_checkpointer() -> SqliteSaver:
    """持久记忆：会话存 sessions.db，重启后可恢复（懒初始化单例）。"""
    global _CHECKPOINTER
    if _CHECKPOINTER is None:
        conn = sqlite3.connect("sessions.db", check_same_thread=False)
        _CHECKPOINTER = SqliteSaver(conn)
    return _CHECKPOINTER


def build_agent(model: ChatOpenAI, mode: str, custom_prompt: str = ""):
    """构建指定模式的 agent 实例，返回 (agent, config)。各模式记忆独立。

    middleware 组合（全部现成组件）：
    - HumanInTheLoopMiddleware   shell 命令人工审批
    - ModelRetryMiddleware       模型限流/超时自动重试
    - ToolRetryMiddleware        工具失败重试
    - TodoListMiddleware         任务清单复述（对抗长上下文目标漂移）
    - FilesystemMiddleware       结构化文件工具（read/write/edit/glob）+
                                 超 20K token 的工具结果自动落盘驱逐
    - SummarizationMiddleware    历史超限时结构化摘要兜底
    """
    cfg = MODE_CONFIGS[mode]
    project_dir = os.path.dirname(os.path.abspath(__file__))
    agent = create_agent(
        model=model,
        tools=get_tools(cfg["tools"]) if cfg["tools"] else [],
        system_prompt=compose_system(cfg["prompt"], custom_prompt),
        middleware=[
            HumanInTheLoopMiddleware(
                interrupt_on={"run_powershell": True, "run_bash": True}
            ),
            ModelRetryMiddleware(max_retries=3),
            ToolRetryMiddleware(max_retries=2),
            TodoListMiddleware(),
            FilesystemMiddleware(
                backend=FilesystemBackend(root_dir=project_dir, virtual_mode=True)
            ),
            SummarizationMiddleware(model=model),
        ],
        checkpointer=_get_checkpointer(),
    )
    # 每个模式独立 thread_id，对话记忆互不污染
    return agent, {"configurable": {"thread_id": f"mode-{mode}"}}


CHAT_MAX_TOOL_ROUNDS = 6  # 两阶段各自的工具调用轮数上限，防止失控


def _chat_loop(
    model: ChatOpenAI, system_content: str, messages: list, tools: list
) -> str:
    """chat 模式的内部对话循环：模型可自主调用文献/数据检索工具。

    与 agent 模式的区别：只挂查询+文献类工具（无 shell），轮数有上限，
    整个循环的中间过程（工具调用与结果）不进入对话历史。
    """
    if not tools:
        return model.invoke([SystemMessage(content=system_content), *messages]).content
    model_bt = model.bind_tools(tools)
    by_name = {t.name: t for t in tools}
    convo = [SystemMessage(content=system_content), *messages]
    resp = None
    for _ in range(CHAT_MAX_TOOL_ROUNDS):
        resp = model_bt.invoke(convo)
        if not getattr(resp, "tool_calls", None):
            return resp.content
        convo.append(resp)
        for tc in resp.tool_calls:
            tool = by_name.get(tc["name"])
            result = tool.invoke(tc["args"]) if tool else f"未知工具: {tc['name']}"
            convo.append(ToolMessage(content=str(result), tool_call_id=tc["id"]))
    return resp.content if resp else ""  # 轮数上限兜底


def chat_reply(
    model: ChatOpenAI, chat_history: list, question: str, custom_prompt: str = ""
) -> str:
    """学术问答模式：两阶段生成，两阶段均可自主调用文献/数据检索工具。

    对用户而言仍是一次性输出；草稿与工具调用的中间过程不进入对话历史。
    custom_prompt 为可选附加提示词，草稿和终答阶段都会叠加。
    环境变量 CHAT_TWO_STAGE=0 时切换为单阶段直答（1~2 次模型调用，更省）。
    """
    tools = get_tools("query")  # 查询类 + 文献类（arXiv/ADS），无 shell
    chat_history.append(HumanMessage(content=question))
    if os.environ.get("CHAT_TWO_STAGE", "1").lower() in ("0", "false", "no", "off"):
        # 单阶段直答：一次调用，模型仍可自主查文献/数据
        reply = _chat_loop(
            model,
            compose_system(CHAT_PROMPT, custom_prompt),
            list(chat_history),  # 全部历史 + 本次提问
            tools,
        )
    else:
        # 阶段一：内部草稿，自由推理试错，可查文献/天体数据
        draft = _chat_loop(
            model,
            compose_system(CHAT_DRAFT_PROMPT, custom_prompt),
            [HumanMessage(content=question)],
            tools,
        )
        # 阶段二：基于草稿输出结构化、标注置信度的最终回答
        reply = _chat_loop(
            model,
            compose_system(CHAT_PROMPT, custom_prompt),
            [
                *chat_history[:-1],  # 此前的对话历史
                HumanMessage(
                    content=f"{question}\n\n--- 内部思考草稿（供核查、修正、取舍）---\n{draft}"
                ),
            ],
            tools,
        )
    chat_history.append(AIMessage(content=reply))
    return reply


def handle_command(text: str, mode: str, custom_prompts: dict, ctx: dict | None = None) -> str:
    """处理斜杠命令，返回（可能更新后的）模式。

    custom_prompts: 模式名 -> 附加提示词。/prompt 命令直接修改此字典；
    agent 实例的重建由 main 循环检测字典变化后自动进行。
    ctx: 运行时共享状态（profiles/current/model/agents/chat_history），
    /model 热切换通过它完成；传 None 时 /model 不可用。
    """
    parts = text.split()
    cmd = parts[0].lower()
    if cmd == "/mode":
        if len(parts) == 1:
            print(f"当前模式: {mode}")
            for name, cfg in MODE_CONFIGS.items():
                mark = "  ← 当前" if name == mode else ""
                print(f"  {name:<9} {cfg['desc']}{mark}")
            print("切换: /mode <名称>")
        elif parts[1] in MODE_CONFIGS:
            if parts[1] != mode:
                print(f"已切换到 {parts[1]} 模式（各模式记忆独立）")
            return parts[1]
        else:
            print(f"未知模式。可用: {'/'.join(MODE_CONFIGS)}")
    elif cmd == "/model":
        if ctx is None:
            print("/model 仅在交互主循环中可用（需要运行时上下文）")
        elif len(parts) == 1:
            cur = ctx["current"]
            print(f"当前模型: {cur} ({ctx['profiles'].get(cur, {}).get('model', '?')})")
            for name, p in ctx["profiles"].items():
                mark = "  ← 当前" if name == cur else ""
                print(f"  {name:<10} {p['model']}{mark}")
            print("切换: /model <档案名>（在 .env 中配置，如 MODEL_NAME_DEEPSEEK=deepseek-chat）")
        elif parts[1] in ctx["profiles"]:
            name = parts[1]
            if name == ctx["current"]:
                print(f"当前已是 {name} ({ctx['profiles'][name]['model']})")
                return mode
            try:
                new_model = build_model(ctx["profiles"][name])
            except ValueError as e:
                print(f"[切换失败] {e}")
                return mode
            old_name = ctx["current"]
            old_model = ctx["profiles"][old_name]["model"]
            ctx["model"] = new_model
            ctx["current"] = name
            note = (
                f"[系统提示] 底层模型已从 {old_model} 切换为 {ctx['profiles'][name]['model']}。"
                "以上历史由前一个模型生成，请无缝衔接当前任务，保持已有结论一致，"
                "不要重复已完成的工作。"
            )
            # agent 模式：交接说明直接写入持久历史（update_state 只改状态，不触发模型调用）
            for rec in ctx["agents"].values():
                try:
                    state = rec["agent"].get_state(rec["config"])
                    if state.values.get("messages"):
                        rec["agent"].update_state(
                            rec["config"], {"messages": [HumanMessage(content=note)]}
                        )
                except Exception:
                    pass
            ctx["agents"].clear()  # 各模式下次使用时用新模型重建；历史在 checkpointer 中不丢失
            if ctx["chat_history"]:  # chat 模式的内存历史同样注入交接说明
                ctx["chat_history"].append(HumanMessage(content=note))
            print(f"已切换模型: {old_name}({old_model}) → {name}({ctx['profiles'][name]['model']})")
            print("对话历史已保留：新模型会读取此前全部进展（含一条交接说明）。")
        else:
            print(
                f"未知模型档案。可用: {'/'.join(ctx['profiles'])}"
                "；先在 .env 中配置 MODEL_NAME_<名字>"
            )
    elif cmd == "/prompt":
        if len(parts) == 1:
            current = custom_prompts.get(mode, "")
            print(f"[{mode}] 附加提示词: {current if current else '（未设置）'}")
            print("设置: /prompt <内容> | 清除: /prompt off")
        elif parts[1].lower() == "off":
            if custom_prompts.pop(mode, None):
                print(f"已清除 {mode} 模式的附加提示词")
            else:
                print(f"{mode} 模式本就没有附加提示词")
        else:
            content = text.split(None, 1)[1].strip()
            custom_prompts[mode] = content
            print(f"已为 {mode} 模式设置附加提示词（对后续回答生效）:\n  {content}")
    elif cmd == "/view":
        rest = text.split(None, 1)
        if len(rest) < 2:
            print("用法: /view <文件> [起始行|起始-结束]    例: /view tools.py 100-150")
        else:
            args = rest[1].split()
            view_file(args[0], parse_range(args[1]) if len(args) > 1 else None)
    elif cmd == "/tree":
        depth = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 2
        list_tree(".", depth)
    elif cmd == "/browse":
        try:
            from browser import run_browser  # 懒导入：未装 textual 不影响主程序启动

            run_browser()
        except Exception as e:
            print(f"[浏览器无法启动] {e}\n[dim]提示: ./.conda/python.exe -m pip install textual[/dim]")
    elif cmd == "/help":
        print(HELP_TEXT)
    else:
        print("未知命令。可用: /mode [模式名]、/model [档案名]、/view [文件]、/tree、/browse、/prompt [内容]、/help")
    return mode


def main():
    profiles = list_model_profiles()
    agents = {}  # 模式名 -> (agent, config)，首次使用时懒创建

    # 启动参数：--chat 等价于 --mode chat；--mode <名称> 进入指定模式；
    # --model <档案名> 指定初始模型；--prompt "<内容>" 为启动模式预置附加提示词
    mode = "agent"
    if "--chat" in sys.argv:
        mode = "chat"
    for i, arg in enumerate(sys.argv):
        if arg == "--mode" and i + 1 < len(sys.argv) and sys.argv[i + 1] in MODE_CONFIGS:
            mode = sys.argv[i + 1]
    start_profile = "default"
    for i, arg in enumerate(sys.argv):
        if arg == "--model" and i + 1 < len(sys.argv) and sys.argv[i + 1] in profiles:
            start_profile = sys.argv[i + 1]

    try:
        model = build_model(profiles.get(start_profile))
    except ValueError as e:
        raise SystemExit(f"{e}——请先复制 .env.example 为 .env，填入 API_KEY 后再运行。")

    custom_prompts: dict[str, str] = {}  # 模式名 -> 附加提示词（可有可无）
    for i, arg in enumerate(sys.argv):
        if arg == "--prompt" and i + 1 < len(sys.argv):
            custom_prompts[mode] = sys.argv[i + 1]

    chat_history: list = []  # chat 模式的独立历史

    # /model 热切换所需的共享运行时状态（handle_command 直接修改其中的字段）
    ctx = {
        "profiles": profiles,
        "current": start_profile,
        "model": model,
        "agents": agents,
        "chat_history": chat_history,
    }

    print("=== 天文 Agent ===")
    cur_prof = profiles.get(start_profile, {})
    print(f"当前模型: {start_profile} ({cur_prof.get('model', '?')})")
    print(f"当前模式: {mode} — {MODE_CONFIGS[mode]['desc']}")
    if mode in custom_prompts:
        print(f"附加提示词: {custom_prompts[mode]}")
    print("所有模式:")
    for name, cfg in MODE_CONFIGS.items():
        print(f"  {name:<9} {cfg['desc']}")
    print("输入 /help 查看命令（含 /prompt 自定义提示词），q 退出\n")

    while True:
        question = input(f"[{mode}] 你: ").strip()
        if not question:
            continue
        if question.lower() in {"q", "quit", "exit"}:
            break
        if question.startswith("/"):
            mode = handle_command(question, mode, custom_prompts, ctx)
            continue
        try:
            if mode == "chat":
                reply = chat_reply(ctx["model"], chat_history, question, custom_prompts.get("chat", ""))
                print(f"\n天文Agent: {reply}\n")
            else:
                # agent 实例懒创建；附加提示词或模型变化后自动重建（历史保留在同 thread_id）
                current_custom = custom_prompts.get(mode, "")
                if (
                    mode not in agents
                    or agents[mode]["custom"] != current_custom
                    or agents[mode]["model"] != ctx["current"]
                ):
                    agent, config = build_agent(ctx["model"], mode, current_custom)
                    agents[mode] = {
                        "agent": agent,
                        "config": config,
                        "custom": current_custom,
                        "model": ctx["current"],
                    }
                agent = agents[mode]["agent"]
                config = agents[mode]["config"]
                result = agent.invoke(
                    {"messages": [{"role": "user", "content": question}]},
                    config=config,
                )
                # shell 工具触发人工审批：agent 暂停，循环处理直到无待审批项
                while "__interrupt__" in result:
                    req = result["__interrupt__"][0].value
                    print("\n[请求执行命令，请审批]")
                    print(req.get("description") or req)
                    choice = input("y=批准 / n=拒绝 / 直接输入替代命令: ").strip()
                    if choice.lower() == "y":
                        decision = {"type": "accept"}
                    elif choice.lower() == "n":
                        decision = {
                            "type": "response",
                            "message": "用户拒绝了这条命令，请改用其他方式完成，或询问用户怎么办。",
                        }
                    else:  # 用户输入了修改后的命令
                        decision = {"type": "response", "message": f"请改用这条命令: {choice}"}
                    result = agent.invoke(Command(resume=[decision]), config=config)
                print(f"\n天文Agent: {result['messages'][-1].content}\n")
        except Exception as e:
            print(f"\n[出错] {e}\n")


if __name__ == "__main__":
    main()
