"""User-invoked browser login, and read-only views of this project's own tabs.

No arbitrary CDP commands or tab enumeration endpoint is exposed to the UI.
Screenshots are transient, opt-in, never saved or included in model context.
"""

import asyncio
import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import aiohttp

from skill_store import no_links
from web_browser import CDP, UserActionRequired, page_url, websocket_url
from workbench_settings import normalize_cdp_endpoint, web_advisor_config


async def browser_info(http, endpoint):
    endpoint = normalize_cdp_endpoint(endpoint)
    async with http.get(endpoint + "/json/version", allow_redirects=False) as response:
        if response.status != 200:
            raise ConnectionError("CDP unavailable")
        blob = await response.content.read(64 * 1024 + 1)
        if len(blob) > 64 * 1024:
            raise ValueError("CDP metadata too large")
        info = json.loads(blob)
    socket = websocket_url(info.get("webSocketDebuggerUrl", ""), endpoint)
    return info, socket


async def connection_status(config):
    config = web_advisor_config(config)
    result = {"cdp_endpoint": config["cdp_endpoint"], "browser_type": config["browser_type"],
              "proxy_mode": config["proxy_mode"], "login_verified": False, "proxy_verified": False}
    try:
        async with aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(total=4)) as http:
            info, _ = await browser_info(http, config["cdp_endpoint"])
        product = info.get("Browser", "Chromium/CDP")
        result.update(connected=True, browser=product[:120] if isinstance(product, str) else "Chromium/CDP",
                      message="CDP 已连接；这不代表网站已登录或所选代理已实际生效。")
    except Exception:
        result.update(connected=False, message="CDP 未连接。普通模式请点击打开登录浏览器；指纹模式请手动启动配置并核对本机 CDP 地址。")
    return result


async def open_login_browser(root, config):
    config = web_advisor_config(config)
    if config["browser_type"] == "fingerprint":
        return {"status": "requires_user", "message": "请在指纹浏览器里手动打开专用配置、启用本机 CDP 并设置代理，再到设置填写它提供的 CDP 地址。没有启动普通浏览器。"}
    if os.name != "nt":
        return {"status": "requires_user", "message": "此登录启动入口仅支持 Windows；请手动启动专用 Chromium 浏览器并启用本机 CDP。"}
    root = Path(root).resolve(strict=True)
    script = no_links(root / "scripts" / "start_web_advisor.ps1", root)
    if not script.is_file():
        raise FileNotFoundError("Browser launcher missing")
    # Only the project's fixed launcher is run, following an explicit UI click.
    helper = await asyncio.create_subprocess_exec(
        "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
        "-PythonExecutable", sys.executable,
        cwd=root, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        out, err = await asyncio.wait_for(helper.communicate(), 12)
    except asyncio.TimeoutError:
        helper.kill()
        await helper.communicate()
        return {"status": "uncertain", "message": "启动结果未知，请先检查是否已有专用窗口，勿连续重复打开。未停止浏览器。"}
    if helper.returncode == 0:
        async with aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(total=0.6)) as http:
            deadline = asyncio.get_running_loop().time() + 8
            while asyncio.get_running_loop().time() < deadline:
                try:
                    await browser_info(http, config["cdp_endpoint"])
                    return {"status": "opened", "connected": True,
                            "message": "专用浏览器已连接。请切换到 Chrome 或 Edge 的专用窗口，在官网手动登录。"}
                except (aiohttp.ClientError, TimeoutError, ConnectionError, ValueError):
                    await asyncio.sleep(0.25)
        return {"status": "not_connected", "connected": False,
                "message": "已发出启动命令，但未连上专用浏览器。若任务栏也没有新窗口，请在桌面双击项目里的「启动网页顾问.bat」，再点「检查连接」。"}
    # Never echo raw helper output, which could contain private paths/page text.
    output = out + err
    if b"CDP port is already occupied" in output:
        connection = await connection_status(config)
        if connection["connected"]:
            return {"status": "already_connected", "connected": True,
                    "message": "浏览器已连接。请从任务栏切换到你已打开的专用窗口。"}
        message = "CDP 端口已被占用，但无法连接浏览器。请检查浏览器设置里的本机 CDP 端口。"
    elif b"Selected local proxy is not listening" in output:
        message = "指定的本机代理未监听；未打开浏览器，也未回退直连。"
    elif b"Chrome or Edge was not found" in output:
        message = "找不到可启动的 Chrome 或 Edge，请检查浏览器是否已安装。"
    else:
        message = "无法打开专用浏览器，请检查 Chrome/Edge、项目启动脚本和保存的设置；未自动安装或停止其他进程。"
    return {"status": "not_opened", "connected": False, "message": message}


async def view_job(job, *, focus=False):
    """Capture/activate only a registered target of this scoped advisor job."""
    if job["status"] not in {"connecting", "sending", "sent", "complete", "uncertain", "resolved"}:
        raise UserActionRequired("此任务还没有可查看的官网标签页")
    target = (job.get("report") or {}).get("browser", {}).get("target_id")
    if not isinstance(target, str) or not target:
        raise UserActionRequired("尚未建立该任务的官网标签页")
    config, provider = web_advisor_config(job["payload"]["config"]), job["request"]["provider"]
    async with asyncio.timeout(7):
        async with aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(total=6)) as http:
            _, socket = await browser_info(http, config["cdp_endpoint"])
            async with http.ws_connect(socket, max_msg_size=2 * 1024 * 1024) as ws:
                cdp = CDP(ws, lambda: False)
                try:
                    info = (await cdp.command("Target.getTargetInfo", {"targetId": target}))["targetInfo"]
                    if info.get("targetId") != target or info.get("type") != "page":
                        raise UserActionRequired("官网标签页已关闭或发生变化")
                    page_url(info.get("url", ""), provider)
                    if focus:
                        await cdp.command("Target.activateTarget", {"targetId": target})
                        return {"status": "activated", "message": "已切换到这次任务的官网标签页，请在专用浏览器窗口中交互。"}
                    session = (await cdp.command("Target.attachToTarget", {"targetId": target, "flatten": True}))["sessionId"]
                    check = "(() => ({url:location.href, sensitive:!!document.querySelector('input[type=password],input[autocomplete=one-time-code],input[autocomplete=current-password],input[autocomplete=new-password]')}))()"
                    state = await cdp.evaluate(check, session)
                    page_url(state["url"], provider)
                    if state["sensitive"]:
                        raise UserActionRequired("检测到密码或验证码输入区域，已暂停画面；请在官网浏览器中手动登录")
                    metrics = await cdp.command("Page.getLayoutMetrics", session=session)
                    viewport = metrics.get("cssVisualViewport") or metrics.get("visualViewport", {})
                    width, height = viewport.get("clientWidth", 0), viewport.get("clientHeight", 0)
                    if not 1 <= width <= 20000 or not 1 <= height <= 20000:
                        raise ValueError("Invalid viewport")
                    shot = await cdp.command("Page.captureScreenshot", {"format": "jpeg", "quality": 60,
                        "captureBeyondViewport": False, "fromSurface": True,
                        "clip": {"x": viewport.get("pageX", 0), "y": viewport.get("pageY", 0),
                                 "width": width, "height": height, "scale": min(1, 1200 / width, 900 / height)}}, session)
                    after = await cdp.evaluate(check, session)
                    url = page_url(after["url"], provider)
                    if after["sensitive"] or url != page_url(state["url"], provider):
                        raise UserActionRequired("页面位置或登录状态改变，已丢弃画面")
                    data = shot.get("data")
                    if not isinstance(data, str) or len(data) > 1400000:
                        raise ValueError("Screenshot too large")
                    blob = base64.b64decode(data, validate=True)
                    if not blob.startswith(b"\xff\xd8\xff") or len(blob) > 1024 * 1024:
                        raise ValueError("Invalid screenshot")
                    return {"status": "preview", "image": "data:image/jpeg;base64," + data, "url": url,
                            "readonly": True, "stored": False, "sent_to_model": False}
                finally:
                    await cdp.close()
