"""Idle auto-close for the dedicated web-advisor browser.

Only the browser instance recorded by scripts/start_web_advisor.ps1 is eligible:
the launcher writes .workbench/web-advisor-browser.json with the PID it started.
A browser the user opened manually (or a fingerprint browser) is never touched,
and nothing is closed while any advisor job is pending or running.
"""

import asyncio
import contextlib
import json
import os
import subprocess
import time
from pathlib import Path

import aiohttp

from web_advisor import WebAdvisorStore
from web_browser import websocket_url
from workbench_settings import WorkbenchSettings, web_advisor_config

MARKER = ".workbench/web-advisor-browser.json"

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _alive_pid(pid, exe_name):
    """PID 存活且映像名与记录一致（防 PID 复用误判）。"""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        size = wintypes.DWORD(32768)
        buf = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return False
        return os.path.basename(buf.value).casefold() == str(exe_name).casefold()
    finally:
        kernel32.CloseHandle(handle)


def recorded_browser(root):
    """返回启动器记录的浏览器信息；不存在、格式不符或进程已退出时返回 None。"""
    try:
        data = json.loads((Path(root) / MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    pid, exe = data.get("pid"), data.get("exe")
    if not isinstance(exe, str) or not exe:
        return None
    if not _alive_pid(pid, os.path.basename(exe)):
        return None
    return data


async def close_browser(root, record, config):
    """先请求 CDP 正常关闭；失败则只终止启动器记录的那个进程树。"""
    endpoint = config["cdp_endpoint"]
    try:
        async with aiohttp.ClientSession(trust_env=False) as http:
            async with http.get(endpoint + "/json/version",
                                timeout=aiohttp.ClientTimeout(total=5)) as response:
                if response.status == 200:
                    info = await response.json()
                    socket_url = websocket_url(info.get("webSocketDebuggerUrl", ""), endpoint)
                    async with http.ws_connect(socket_url, timeout=5) as socket:
                        await socket.send_json({"id": 1, "method": "Browser.close"})
                    await asyncio.sleep(2)
                    if not _alive_pid(record.get("pid"), os.path.basename(record["exe"])):
                        return "cdp"
    except (OSError, ValueError, aiohttp.ClientError, asyncio.TimeoutError):
        pass
    pid = record.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return None
    if os.name == "nt":
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=15)
    else:
        with contextlib.suppress(OSError):
            os.kill(pid, 15)
    return "signal"


async def maybe_close(root, state):
    """一次判定：满足条件就关闭并返回说明，否则返回 None。

    state 是跨轮次保留的 dict，只用 "since" 记录本次空闲计时起点。
    """
    try:
        config = web_advisor_config(WorkbenchSettings(root).load().get("web_advisor"))
    except ValueError:
        state.pop("since", None)
        return None
    minutes = config.get("auto_close_minutes", 0)
    # 指纹浏览器与手动启动的浏览器不归我们管。
    if not minutes or config.get("browser_type") != "dedicated":
        state.pop("since", None)
        return None
    record = await asyncio.to_thread(recorded_browser, root)
    if record is None:
        state.pop("since", None)
        return None
    store = WebAdvisorStore(root)
    if await asyncio.to_thread(store.has_unfinished):
        state.pop("since", None)
        return None
    last = await asyncio.to_thread(store.last_activity)
    now = time.time()
    since = max(state.get("since") or now, last)
    state["since"] = since
    if now - since < minutes * 60:
        return None
    how = await close_browser(root, record, config)
    state.pop("since", None)
    return {"minutes": minutes, "how": how}


async def autoclose_loop(root, interval=30):
    """工作台服务运行期间每 interval 秒判定一次。"""
    state = {}
    while True:
        await asyncio.sleep(interval)
        try:
            result = await maybe_close(root, state)
        except asyncio.CancelledError:
            raise
        except Exception:
            continue
        if result:
            print(f"[网页顾问] 空闲 {result['minutes']} 分钟，已关闭专用浏览器（{result['how']}）")