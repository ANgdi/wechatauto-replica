# -*- coding: utf-8 -*-
"""自动回复的会话记忆：每个会话（私聊/群）一份，持久化到 JSON。

设计：

- **按 chat username 隔离**：私聊是 wxid，群是 ``xxx@chatroom``，互不串味；
- **长度上限**：``max_turns`` 轮（一轮 = 用户 + 助手两条）且总字符不超过
  ``max_chars``，超出从最旧的开始丢；
- 只保存 ``role`` / ``content``，不写时间戳等无关字段；
- 每次追加后原子落盘（tmp + replace），进程重启后还在；
- 内容只在本机，不会额外上传（走 AI 时才会随请求发给模型服务商）。
"""
from __future__ import annotations

import json
import os
import threading
from typing import Dict, List, Optional


class ChatMemory:
    def __init__(self, path: str, max_turns: int = 10, max_chars: int = 3000):
        self.path = path
        self.max_turns = max(1, int(max_turns))
        self.max_chars = max(200, int(max_chars))
        self._lock = threading.RLock()
        self._data: Dict[str, List[dict]] = self._load()

    # -- 持久化 --------------------------------------------------------
    def _load(self) -> Dict[str, List[dict]]:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            return {}
        chats = raw.get("chats") if isinstance(raw, dict) else None
        if not isinstance(chats, dict):
            return {}
        out: Dict[str, List[dict]] = {}
        for chat, msgs in chats.items():
            if not isinstance(msgs, list):
                continue
            clean = [m for m in msgs
                     if isinstance(m, dict)
                     and m.get("role") in ("user", "assistant")
                     and isinstance(m.get("content"), str)]
            if clean:
                out[str(chat)] = clean
        return out

    def _save(self) -> None:
        payload = {"version": 1, "chats": self._data}
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=0)
            os.replace(tmp, self.path)
        except OSError:
            pass

    # -- 读写 ------------------------------------------------------------
    def get(self, chat: str) -> List[dict]:
        """返回该会话的历史消息副本（可直接放进 API 的 messages）。"""
        with self._lock:
            return [dict(m) for m in self._data.get(chat, [])]

    def add(self, chat: str, role: str, content: str) -> None:
        content = (content or "").strip()
        if not content or role not in ("user", "assistant"):
            return
        with self._lock:
            arr = self._data.setdefault(chat, [])
            arr.append({"role": role, "content": content})
            self._trim(arr)
            self._save()

    def _trim(self, arr: List[dict]) -> None:
        # 1) 轮数上限：保留最近 max_turns 轮（2*max_turns 条）
        keep = self.max_turns * 2
        if len(arr) > keep:
            del arr[:len(arr) - keep]
        # 2) 字符上限：从最旧开始丢，至少保留最近一轮
        total = sum(len(m["content"]) for m in arr)
        while total > self.max_chars and len(arr) > 2:
            total -= len(arr.pop(0)["content"])

    def clear(self, chat: Optional[str] = None) -> None:
        with self._lock:
            if chat is None:
                self._data.clear()
            else:
                self._data.pop(chat, None)
            self._save()

    def stats(self, chat: str) -> dict:
        with self._lock:
            arr = self._data.get(chat, [])
            return {
                "messages": len(arr),
                "chars": sum(len(m["content"]) for m in arr),
                "max_turns": self.max_turns,
                "max_chars": self.max_chars,
            }
