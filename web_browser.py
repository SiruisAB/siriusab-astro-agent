"""Small CDP client for approved text-only consultations in newly created tabs.

No browser launch, file upload, cookies/storage access, remote tool execution or
CAPTCHA bypass. Page selectors are deliberately fail-closed when a UI changes.
"""

import asyncio
import json
from contextlib import suppress
from urllib.parse import urlsplit, urlunsplit

import aiohttp
from workbench_settings import WEB_ADVISOR_URLS

START_URLS = WEB_ADVISOR_URLS
HOSTS = {provider: urlsplit(url).hostname for provider, url in START_URLS.items()}
NEW_CHAT_PATHS = {"chatgpt": {"", "/"}, "claude": {"", "/", "/new"}, "gemini": {"/app", "/app/"}}
SELECTORS = {
    "chatgpt": {"input": ['#prompt-textarea', 'textarea[data-id="root"]'],
                "answer": ['[data-message-author-role="assistant"]'],
                "send": ['button[data-testid="send-button"]', 'button[aria-label="Send prompt"]', 'button[aria-label="发送提示"]'],
                "stop": ['button[data-testid="stop-button"]', 'button[aria-label="Stop generating"]', 'button[aria-label="停止生成"]']},
    "claude": {"input": ['.ProseMirror[contenteditable="true"]', '[contenteditable="true"][role="textbox"]'],
               "answer": ['[data-testid="assistant-message"]', '.font-claude-response'],
               "send": ['button[aria-label="Send message"]', 'button[aria-label="Send Message"]', 'button[aria-label="发送消息"]', 'button[data-testid="send-button"]'],
               "stop": ['button[aria-label="Stop Response"]', 'button[aria-label="Stop response"]', 'button[aria-label="停止回复"]']},
    "gemini": {"input": ['rich-textarea .ql-editor[contenteditable="true"]', '.input-area [contenteditable="true"][role="textbox"]'],
               "answer": ['model-response message-content', 'model-response .markdown', 'model-response .model-response-text'],
               "complete": ['copy-button button', 'button[data-test-id="copy-button"]'],
               "send": ['button.send-button', 'button[aria-label="Send message"]', 'button[aria-label="发送消息"]', 'button[aria-label="发送"]'],
               "stop": ['button.stop-button', 'button[aria-label="Stop response"]', 'button[aria-label="Stop generating"]', 'button[aria-label="停止回复"]', 'button[aria-label="停止生成"]']},
}


class UserActionRequired(RuntimeError):
    requires_user = True


def websocket_url(value, endpoint):
    parsed, base = urlsplit(value), urlsplit(endpoint)
    if (parsed.scheme != "ws" or parsed.hostname != base.hostname or parsed.port != base.port
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or not parsed.path.startswith("/devtools/browser/")):
        raise ValueError("浏览器 WebSocket 地址不属于配置的本机 CDP 服务")
    return value


def page_url(value, provider):
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.hostname != HOSTS[provider] or parsed.port not in {None, 443}
            or parsed.username or parsed.password):
        raise UserActionRequired("网页已离开批准的网站")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


class CDP:
    def __init__(self, websocket, cancelled):
        self.websocket, self.cancelled = websocket, cancelled
        self.counter, self.pending = 0, {}
        self.reader = asyncio.create_task(self.receive())

    async def receive(self):
        try:
            async for message in self.websocket:
                if message.type != aiohttp.WSMsgType.TEXT:
                    continue
                data = json.loads(message.data)
                future = self.pending.get(data.get("id"))
                if future and not future.done():
                    if "error" in data:
                        future.set_exception(RuntimeError("浏览器拒绝了 CDP 命令"))
                    else:
                        future.set_result(data.get("result", {}))
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("CDP 连接已断开"))

    async def command(self, method, params=None, session=None):
        if self.cancelled():
            raise RuntimeError("用户已停止任务")
        self.counter += 1
        identifier = self.counter
        future = asyncio.get_running_loop().create_future()
        self.pending[identifier] = future
        data = {"id": identifier, "method": method, "params": params or {}}
        if session:
            data["sessionId"] = session
        try:
            await self.websocket.send_json(data)
            return await asyncio.wait_for(future, 8)
        finally:
            self.pending.pop(identifier, None)

    async def evaluate(self, expression, session):
        response = await self.command("Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True}, session)
        if response.get("exceptionDetails") or "value" not in response.get("result", {}):
            raise RuntimeError("网页状态读取失败")
        return response["result"]["value"]

    async def close(self):
        self.reader.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await self.reader


def state_script(provider):
    selectors = json.dumps(SELECTORS[provider])
    return r"""(() => {
      const s = SELECTORS;
      const visible = e => !!e && !!e.getClientRects().length;
      const first = choices => choices.map(q => [...document.querySelectorAll(q)].find(visible)).find(Boolean);
      const input = first(s.input), send = first(s.send), stop = first(s.stop);
      let inputText = input ? (input.value ?? input.innerText ?? '') : '';
      if (input?.matches('rich-textarea .ql-editor') && input.childNodes.length &&
          [...input.childNodes].every(p => p.nodeName === 'P' &&
            ([...p.childNodes].every(n => n.nodeType === Node.TEXT_NODE) ||
              (p.childNodes.length === 1 && p.firstChild.nodeName === 'BR')))) {
        // Gemini's Quill paragraphs represent individual input lines. innerText adds a blank line between them.
        inputText = [...input.children].map(p => p.textContent).join('\n');
      }
      let answers = [];
      for (const q of s.answer) { answers = [...document.querySelectorAll(q)].filter(visible); if (answers.length) break; }
      const last = answers.at(-1);
      const scope = last?.closest('model-response, [data-testid^="conversation-turn"]') || last?.parentElement;
      const action = scope && ([...scope.querySelectorAll('button')].some(b => {
        const label = (b.getAttribute('aria-label') || b.getAttribute('data-testid') || '').toLowerCase();
        return /copy.*(?:response|message|turn)|copy-turn-action-button|复制.*(?:回复|消息)/.test(label);
      }) || (s.complete || []).some(q => [...scope.querySelectorAll(q)].some(visible)));
      const text = last?.innerText || '';
      return {url: location.href, input: !!input, inputText,
              send: !!send && !send.disabled && send.getAttribute('aria-disabled') !== 'true',
              busy: !!stop, count: answers.length, answer: text.slice(0, 96000), overflow: text.length > 96000,
              completedMarker: !!action};
    })()""".replace("SELECTORS", selectors, 1)


def action_script(provider, action):
    selectors = json.dumps(SELECTORS[provider]["input" if action == "focus" else "send"])
    operation = "e.focus(); return true;" if action == "focus" else "if(e.disabled || e.getAttribute('aria-disabled') === 'true') return false; e.click(); return true;"
    origin = json.dumps("https://" + HOSTS[provider])
    return f"""(() => {{if(location.origin!=={origin})return false; const q={selectors}; let e; for(const s of q) {{e=[...document.querySelectorAll(s)].find(x=>x.getClientRects().length); if(e)break;}} if(!e)return false; {operation}}})()"""


async def consult(provider, payload, phase, cancelled):
    if provider not in HOSTS:
        raise ValueError("不支持的网页顾问")
    config, cdp, target, wrote = payload["config"], None, None, False
    endpoint = config["cdp_endpoint"]
    async with asyncio.timeout(config["timeout"]):
        async with aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(total=None, connect=5)) as http:
            async with http.get(endpoint + "/json/version", allow_redirects=False, timeout=aiohttp.ClientTimeout(total=5)) as response:
                if response.status != 200:
                    raise ConnectionError("CDP 服务不可用")
                blob = await response.content.read(64 * 1024 + 1)
                if len(blob) > 64 * 1024:
                    raise ValueError("CDP 元数据过大")
                info = json.loads(blob)
            socket_url = websocket_url(info.get("webSocketDebuggerUrl", ""), endpoint)
            async with http.ws_connect(socket_url, max_msg_size=512 * 1024) as socket:
                cdp = CDP(socket, cancelled)
                try:
                    result = await cdp.command("Target.createTarget", {"url": START_URLS[provider], "background": True})
                    target = result["targetId"]
                    phase("opened", target_id=target, url=START_URLS[provider])
                    attached = await cdp.command("Target.attachToTarget", {"targetId": target, "flatten": True})
                    session = attached["sessionId"]
                    ready_until = asyncio.get_running_loop().time() + 25
                    while True:
                        state = await cdp.evaluate(state_script(provider), session)
                        if state["url"] == "about:blank":
                            if asyncio.get_running_loop().time() >= ready_until:
                                raise UserActionRequired("网页没有完成导航")
                            await asyncio.sleep(0.5)
                            continue
                        url = page_url(state["url"], provider)
                        if state["input"]:
                            break
                        if asyncio.get_running_loop().time() >= ready_until:
                            raise UserActionRequired("请手动登录或检查网页布局")
                        await asyncio.sleep(0.5)
                    if state["count"] or state["inputText"].strip() or urlsplit(url).path not in NEW_CHAT_PATHS[provider]:
                        raise UserActionRequired("拒绝覆盖草稿或混入已有网页对话")
                    if not await cdp.evaluate(action_script(provider, "focus"), session):
                        raise UserActionRequired("没有匹配的网页输入框")
                    # Text can be observed/autosaved by the page before Send is clicked.
                    phase("sending")
                    wrote = True
                    await cdp.command("Input.insertText", {"text": payload["body"]}, session)
                    send_until = asyncio.get_running_loop().time() + 8
                    while True:
                        state = await cdp.evaluate(state_script(provider), session)
                        page_url(state["url"], provider)
                        if state["inputText"].replace("\r\n", "\n") != payload["body"].replace("\r\n", "\n"):
                            raise RuntimeError("网页输入与批准的文本不一致")
                        if state["send"]:
                            break
                        if asyncio.get_running_loop().time() >= send_until:
                            raise RuntimeError("发送按钮未就绪")
                        await asyncio.sleep(0.3)
                    if not await cdp.evaluate(action_script(provider, "send"), session):
                        raise RuntimeError("网页未确认发送操作")
                    last_url = START_URLS[provider]
                    last, unchanged_since, saw_busy = "", asyncio.get_running_loop().time(), False
                    sent = False
                    delivery_until = asyncio.get_running_loop().time() + 15
                    while True:
                        state = await cdp.evaluate(state_script(provider), session)
                        url = page_url(state["url"], provider)
                        if url != last_url:
                            phase("location", url=url)
                            last_url = url
                        if not sent:
                            if state["count"] or state["busy"] or urlsplit(url).path not in NEW_CHAT_PATHS[provider]:
                                phase("sent")
                                sent = True
                            elif asyncio.get_running_loop().time() >= delivery_until:
                                raise RuntimeError("点击发送后网页未显示新对话或开始回复")
                        if state["overflow"]:
                            raise ValueError("网页回答过大")
                        saw_busy = saw_busy or state["busy"]
                        if state["answer"] != last:
                            last, unchanged_since = state["answer"], asyncio.get_running_loop().time()
                            if last:
                                phase("answer", answer=last)
                        stable = asyncio.get_running_loop().time() - unchanged_since >= 3
                        if (state["count"] > 0 and last.strip() and stable and not state["busy"]
                                and (state["completedMarker"] or saw_busy)):
                            return {"answer": last, "url": url}
                        await asyncio.sleep(0.5)
                finally:
                    # Successful and unknown deliveries retain only our own tab so
                    # the user can inspect the actual official conversation.
                    if target and not wrote and not cancelled():
                        with suppress(Exception):
                            await cdp.command("Target.closeTarget", {"targetId": target})
                    await cdp.close()
