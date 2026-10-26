# -*- coding: utf-8 -*-
"""可选的 AI 回复钩子（OpenAI 兼容接口，不用额外依赖）。

只负责「给一段对方消息，返回一段回复文本」，不碰微信、不碰线程。
默认从环境变量读配置：

    WECHATAUTO_AI_KEY     必填，API Key
    WECHATAUTO_AI_BASE    接口地址，默认 https://api.deepseek.com/v1
    WECHATAUTO_AI_MODEL   模型名，默认 deepseek-chat
    WECHATAUTO_AI_SYSTEM  系统提示词（可选）
    WECHATAUTO_AI_TIMEOUT 超时秒数，默认 20

兼容 DeepSeek / OpenAI / Ollama / vLLM 等 `/chat/completions` 接口。
Key 只从环境变量读，不写进代码、不落盘。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Callable, Optional

DEFAULT_BASE = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_SYSTEM = (
    "你是微信聊天助手。用中文口语化地简短回复，尽量不超过 80 字；"
    "不要提到自己是 AI、模型或自动回复。"
)


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or "").strip() or default


def _chat(base: str, key: str, model: str, system: str,
          text: str, sender: str, chat: str, timeout: float) -> Optional[str]:
    url = base.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content":
                "聊天对象：%s\n发送者：%s\n对方消息：%s\n请直接给出回复内容。"
                % (chat, sender, text)},
        ],
        "temperature": 0.7,
        "max_tokens": 200,
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
                  ) -> Optional[Callable[[str, str, str], Optional[str]]]:
    """构造 ``fn(text, sender, chat) -> reply|None``；没配 Key 返回 None。"""
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

    def fn(text: str, sender: str, chat: str) -> Optional[str]:
        return _chat(base, key, model, system, text, sender, chat, timeout)

    return fn
