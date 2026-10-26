# -*- coding: utf-8 -*-
"""可选的 AI 回复钩子（OpenAI 兼容接口，不用额外依赖）。

只负责「给一段对方消息，返回一段回复文本」，不碰微信、不碰线程。

配置优先读环境变量，其次读仓库根目录的 ``.env``（``.env`` 已 gitignore）：

    WECHATAUTO_AI_KEY     必填，API Key
    WECHATAUTO_AI_BASE    接口地址，默认 https://api.deepseek.com/v1
    WECHATAUTO_AI_MODEL   模型名，默认 deepseek-chat
    WECHATAUTO_AI_SYSTEM  系统提示词（可选）
    WECHATAUTO_AI_TIMEOUT 超时秒数，默认 20

兼容 DeepSeek / OpenAI / Ollama / vLLM 等 `/chat/completions` 接口。
Key 只从环境变量 / .env 读，不写进代码；.env 不会被提交。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Callable, Optional

from wechatauto.env import load_dotenv

DEFAULT_BASE = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_SYSTEM = (
    "你是微信聊天助手。用中文口语化地简短回复，尽量不超过 80 字；"
    "不要提到自己是 AI、模型或自动回复。"
)


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or "").strip() or default


def _chat(base: str, key: str, model: str, system: str,
          text: str, sender: str, chat: str, timeout: float,
          history: Optional[list] = None) -> Optional[str]:
    url = base.rstrip("/") + "/chat/completions"
    messages = [{"role": "system", "content": system}]
    for m in history or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = m.get("content")
        if role in ("user", "assistant") and isinstance(content, str) and content.strip():
            messages.append({"role": role, "content": content})
    messages.append({
        "role": "user",
        "content": "聊天对象：%s\n发送者：%s\n对方消息：%s\n请直接给出回复内容。"
                   % (chat, sender, text),
    })
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 300,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        content = content.strip()
        return content or None
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError, IndexError):
        return None


def make_reply_fn(base: Optional[str] = None, key: Optional[str] = None,
                  model: Optional[str] = None, system: Optional[str] = None,
                  timeout: Optional[float] = None
                  ) -> Optional[Callable[..., Optional[str]]]:
    """构造 ``fn(text, sender, chat) -> reply|None``；没配 Key 返回 None。"""
    load_dotenv()          # .env 只补缺失项，系统环境变量优先
    key = (key or _env("WECHATAUTO_AI_KEY"))
    if not key:
        return None
    base = base or _env("WECHATAUTO_AI_BASE", DEFAULT_BASE)
    model = model or _env("WECHATAUTO_AI_MODEL", DEFAULT_MODEL)
    system = system or _env("WECHATAUTO_AI_SYSTEM", DEFAULT_SYSTEM)
    try:
        timeout = float(timeout if timeout is not None
                        else _env("WECHATAUTO_AI_TIMEOUT", "20"))
    except ValueError:
        timeout = 20.0

    def fn(text: str, sender: str, chat: str,
           history: Optional[list] = None) -> Optional[str]:
        return _chat(base, key, model, system, text, sender, chat,
                     timeout, history=history)

    return fn
