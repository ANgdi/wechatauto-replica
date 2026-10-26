# -*- coding: utf-8 -*-
"""自动回复示例：数据库监听（只读轮询）+ 规则匹配 + 微信窗口发送。

用法::

    # 1) 先干跑：只打印命中的规则，绝不点微信窗口
    python -m wechatauto.demo_auto_reply --chat 文件传输助手 --dry-run

    # 2) 规则文件 + 真发（默认回读数据库确认）
    python -m wechatauto.demo_auto_reply --rules auto_reply_rules.json

    # 3) 监听全部会话，未命中时给一句兜底
    python -m wechatauto.demo_auto_reply --all --default-reply "我现在不在，稍后回复"

    # 4) 不依赖微信的规则自检
    python -m wechatauto.demo_auto_reply --self-test

设计要点
--------
* **读**走 ``db.Listener``（只读轮询，不点界面）；**写**走一个独立的发送线程。
  回调里只做规则匹配和入队，绝不阻塞轮询线程。
* 发送前**必须显式绑定微信主窗口 HWND**并按进程名复核：本机实测
  ``WeChatGUI()`` 的自动发现会把标题里带「微信」的 Chrome 标签页当成微信
  （标题命中 +5、尺寸 +2 就过线），随后在错误的窗口上做 OCR / 点击。
* 默认只回**文本**消息、只回**别人**发的消息（``sender_username`` 对比本机
  wxid）；群聊默认要求 @ 或前缀，避免在群里刷屏。
* 节奏由 ``wechatauto.rhythm`` 统一节流（默认 natural：写动作 2.5~6s 间隔），
  本脚本另加每会话冷却与每分钟上限，双保险。
* 发送结果用 ``verify=True`` 从数据库回读确认；**回读不确定时不重发**，
  避免同一条消息发两遍（库的发送链路本身就有这个约束）。
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

try:
    os.system("chcp 65001 >nul 2>&1")
except Exception:
    pass
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass

# 群聊正文形如 "<发送者>:\n正文"（图片等非文本消息也带这个前缀）。
# 发送者不一定是 wxid_ 开头：自定义微信号（如 z84772180）也会出现在这里，
# 所以按「2 个以上合法用户名字符 + 冒号 + 换行」通用匹配，避免前缀漏剥。
_GROUP_PREFIX_RE = re.compile(r"^\s*[0-9A-Za-z_@.\-]{2,}:[ \t]*\r?\n")
# 微信 @ 后面常跟 U+2005（四分之一空格）而不是普通空格
_MENTION_SPACES = str.maketrans({"\u2005": " ", "\u00a0": " ", "\u3000": " "})

DEFAULT_IGNORE = (
    "filehelper",                  # 文件传输助手：自己给自己发，别回
    "brandsessionholder",          # 公众号会话占位
    "@placeholder_foldgroup",      # 折叠的群占位
    "weixin",                      # 系统通知
)
DEFAULT_SERVICE_PREFIXES = ("gh_",)  # 公众号（官方账号）默认不回
DEFAULT_SERVICE_SUFFIXES = ("@openim",)


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
class Log:
    def __init__(self, path: str = ""):
        self.path = path
        self._lock = threading.Lock()
        if path:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

    def __call__(self, msg: str) -> None:
        line = "[%s] %s" % (time.strftime("%m-%d %H:%M:%S"), msg)
        with self._lock:
            print(line, flush=True)
            if self.path:
                try:
                    with open(self.path, "a", encoding="utf-8") as f:
                        f.write(line + "\n")
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# 规则
# ---------------------------------------------------------------------------
@dataclass
class Rule:
    """一条回复规则。

    match 里的每一项都可以是普通子串、正则（regex=True）或整句（exact=True）。
    chats 为空表示对所有会话生效；否则按会话 username 精确匹配。
    """

    match: List[str]
    reply: str
    regex: bool = False
    exact: bool = False
    ignore_case: bool = True
    chats: List[str] = field(default_factory=list)

    def hit(self, text: str, chat: str) -> bool:
        if self.chats and chat not in self.chats:
            return False
        hay = text.casefold() if self.ignore_case else text
        for pat in self.match:
            if self.exact:
                ok = hay.strip() == (pat.casefold() if self.ignore_case else pat)
            elif self.regex:
                flags = re.IGNORECASE if self.ignore_case else 0
                ok = re.search(pat, text, flags) is not None
            else:
                need = pat.casefold() if self.ignore_case else pat
                ok = need in hay
            if ok:
                return True
        return False


@dataclass
class RuleSet:
    rules: List[Rule] = field(default_factory=list)
    default_reply: str = ""
    group_prefix: str = "!"
    group_require_mention: bool = True
    group_mention_any: bool = False

    @classmethod
    def from_json(cls, path: str) -> "RuleSet":
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        rs = cls(
            default_reply=(data.get("default_reply") or ""),
            group_prefix=(data.get("group_prefix") or "!"),
            group_require_mention=bool(data.get("group_require_mention", True)),
            group_mention_any=bool(data.get("group_mention_any", False)),
        )
        for item in data.get("rules") or []:
            match = item.get("match")
            if isinstance(match, str):
                match = [match]
            if not match or not item.get("reply"):
                continue
            rs.rules.append(Rule(
                match=[str(x) for x in match],
                reply=str(item["reply"]),
                regex=bool(item.get("regex", False)),
                exact=bool(item.get("exact", False)),
                ignore_case=bool(item.get("ignore_case", True)),
                chats=list(item.get("chats") or []),
            ))
        return rs

    def match(self, text: str, chat: str) -> Optional[str]:
        for rule in self.rules:
            if rule.hit(text, chat):
                return rule.reply
        return None


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def find_wechat_hwnd() -> int:
    """只认 ``Weixin.exe`` 进程、类名 Qt 主窗、尺寸够大的可见窗口。

    绝不用「标题里含微信」来认窗口：Chrome 标签页标题恰好带「微信」时，
    库的自动发现会选中它，然后在错误的窗口上 OCR/点击。
    """
    try:
        import psutil
        import win32gui
        import win32process
    except Exception:
        return 0

    best = []

    def cb(h, _):
        try:
            if not win32gui.IsWindowVisible(h):
                return True
            _, pid = win32process.GetWindowThreadProcessId(h)
            if (psutil.Process(pid).name() or "").lower() != "weixin.exe":
                return True
            if not win32gui.GetClassName(h).startswith("Qt51514QWindowIcon"):
                return True
            left, top, right, bottom = win32gui.GetWindowRect(h)
            w, ht = right - left, bottom - top
            # 登录窗 / 独立小窗都小于这个尺寸；主聊天窗一定更大
            if w < 640 or ht < 520:
                return True
            best.append((w * ht, h))
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, None)
    best.sort(reverse=True)
    return best[0][1] if best else 0


def pid_is_weixin(pid: int) -> bool:
    try:
        import psutil
        return (psutil.Process(pid).name() or "").lower() == "weixin.exe"
    except Exception:
        return False


def find_hwnd_for_account(account: str, db_dir: str = "",
                          attempts: int = 4, delay: float = 1.0) -> int:
    """按账号目录找**它自己的**微信主窗口（多开时必须）。

    多开时两个窗口标题都叫「微信」，只按尺寸挑最大的会绑到另一个号上。
    这里先用进程打开的文件把账号目录映射到 PID，再在该 PID 的 Qt 主窗里挑。

    微信刚启动/刚登录时，进程可能还没打开 `db_storage` 里的文件，`open_files`
    会短暂看不到账号 → 以前会直接判定“找不到窗口”。所以这里重试几次
    （默认 4 次、间隔 1 秒），给客户端一点时间把库文件句柄建起来。
    """
    try:
        import psutil
        import win32gui
        import win32process
    except Exception:
        return 0
    from wechatauto.db import auto_detect_db_dir

    root = db_dir or auto_detect_db_dir()
    if not root:
        return 0
    base = os.path.normcase(os.path.abspath(os.path.join(root, account)))

    for attempt in range(max(1, attempts)):
        pids = set()
        for p in psutil.process_iter(["pid", "name"]):
            if (p.info.get("name") or "").lower() != "weixin.exe":
                continue
            try:
                for f in p.open_files():
                    if os.path.normcase(os.path.abspath(f.path)).startswith(base + os.sep):
                        pids.add(p.info["pid"])
                        break
            except Exception:
                continue
        if pids:
            best = []

            def cb(h, _):
                try:
                    _, pid = win32process.GetWindowThreadProcessId(h)
                    if pid not in pids:
                        return True
                    if not win32gui.IsWindowVisible(h):
                        return True
                    if not win32gui.GetClassName(h).startswith("Qt51514QWindowIcon"):
                        return True
                    # 最小化的窗口 GetWindowRect 是 -16000 之类的值，用正常位置尺寸评分
                    normal = win32gui.GetWindowPlacement(h)[4]
                    w, ht = normal[2] - normal[0], normal[3] - normal[1]
                    if w < 640 or ht < 520:
                        return True
                    best.append((w * ht, h))
                except Exception:
                    pass
                return True

            win32gui.EnumWindows(cb, None)
            if best:
                best.sort(reverse=True)
                return best[0][1]
        if attempt + 1 < attempts:
            time.sleep(delay)
    return 0


def clean_text(text: str, is_group: bool) -> str:
    """去掉群聊正文的发送者前缀，并归一化 @ 后面的特殊空格。"""
    text = text or ""
    if is_group:
        text = _GROUP_PREFIX_RE.sub("", text, count=1)
    return text.translate(_MENTION_SPACES)


def resolve_chat(db, raw: str) -> Optional[str]:
    """把昵称/备注/群名解析成会话 username；解析不到返回 None。"""
    raw = (raw or "").strip()
    if not raw:
        return None
    if raw in ("filehelper", "文件传输助手"):
        return "filehelper"
    if raw.startswith("wxid_") or raw.endswith("@chatroom"):
        return raw
    # 会话列表里直接有同名字段（部分群名只在 session.db）
    try:
        nickmap = db.nickname_map()
    except Exception:
        nickmap = {}
    for s in db.get_sessions(limit=500):
        if s["username"] == raw:
            return raw
        if nickmap.get(s["username"]) == raw:
            return s["username"]
    hits = db.search_contact(raw)
    # 自定义微信号（如 lee65786）既不是 wxid_ 开头，也不一定出现在会话列表里，
    # 但 contact.db 的 username 精确相等时就是它本身。
    for h in hits:
        if h.get("username") == raw:
            return raw
    exact = [h for h in hits if raw in (h.get("nick_name"), h.get("remark"))]
    if len(exact) == 1:
        return exact[0]["username"]
    gid = db.group_name_to_id(raw)
    if gid:
        return gid
    return None


def display_name_of(db, username: str) -> str:
    """会话 username → 微信界面上能搜到的显示名（备注 > 昵称 > 群名）。"""
    if username == "filehelper":
        return "文件传输助手"
    return db.get_nickname(username) or username


def render_template(tpl: str, sender: str, chat: str, content: str) -> str:
    return (tpl.replace("{sender}", sender)
               .replace("{chat}", chat)
               .replace("{content}", content)
               .replace("{time}", time.strftime("%H:%M")))


# “开始新对话”命令：清空**当前会话**的记忆；群里需要带 ! 前缀（! /new 也行）
RESET_COMMANDS = {
    "/new", "/reset", "/重置对话", "/重置", "重置对话", "新对话", "开始新对话",
}


def is_reset_command(text: str) -> bool:
    return (text or "").strip().casefold() in RESET_COMMANDS


def cap_text(text: str, limit: int) -> str:
    """按字符数截断（中文按 1 个字算）；``limit<=0`` 不截断。"""
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:max(0, limit - 1)].rstrip() + "…"


# ---------------------------------------------------------------------------
# 发送线程
# ---------------------------------------------------------------------------
class Sender(threading.Thread):
    """唯一发送线程：构造并独占 WeChatGUI，串行执行所有回复。"""

    def __init__(self, hwnd: Optional[int], log: Log, verify: bool = False,
                 dry_run: bool = False):
        super().__init__(name="wx-auto-reply-sender", daemon=True)
        self.hwnd = hwnd
        self.log = log
        self.verify = verify
        self.dry_run = dry_run
        self.q: "queue.Queue[tuple]" = queue.Queue(maxsize=50)
        self._stop = threading.Event()
        self.gui = None

    def submit(self, chat_username: str, display: str, text: str) -> bool:
        try:
            self.q.put_nowait((chat_username, display, text))
            return True
        except queue.Full:
            self.log("发送队列已满，丢弃本条回复：%s" % display)
            return False

    def _ensure_gui(self):
        if self.gui is not None:
            return self.gui
        if not self.hwnd:
            self.hwnd = find_wechat_hwnd()
        if not self.hwnd:
            raise RuntimeError("找不到微信主窗口（Weixin.exe 是否已登录？）")
        from wechatauto.guia import WeChatGUI
        gui = WeChatGUI(hwnd=self.hwnd)
        if not pid_is_weixin(gui.pid):
            raise RuntimeError("绑定的窗口进程不是 Weixin.exe（hwnd=%s pid=%s），拒绝发送"
                               % (gui.main_hwnd, gui.pid))
        self.gui = gui
        self.log("发送线程已绑定微信主窗口 hwnd=%s pid=%s"
                 % (gui.main_hwnd, gui.pid))
        return gui

    def run(self):
        while not self._stop.is_set():
            try:
                chat_username, display, text = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            if self.dry_run:
                self.log("[dry-run] 将回复 %s（%s）：%s"
                         % (display, chat_username, text))
                continue
            try:
                gui = self._ensure_gui()
                started = time.time()
                resp = gui.send_msg(text, display, verify=self.verify)
                status = getattr(resp, "get", lambda *_: None)("status") if isinstance(resp, dict) else None
                msg = resp.get("message") if isinstance(resp, dict) else str(resp)
                self.log("回复 %s -> %s（%.1fs，verify=%s）"
                         % (display, msg, time.time() - started, self.verify))
                if status != "成功":
                    # 发送链路明确说过「已尝试但未确认」时不能重发；这里只记录，不重试。
                    self.log("  发送未确认，按防重复原则不自动重试：%s" % msg)
            except Exception as exc:
                self.log("回复 %s 失败：%s: %s" % (display, type(exc).__name__, exc))
                # 窗口被关/换了，下一轮重新绑定
                self.gui = None
            finally:
                self.q.task_done()

    def stop(self, release: bool = True):
        self._stop.set()
        if release and self.gui is not None:
            try:
                self.gui.release_foreground(with_blockers=True)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
class AutoReplier:
    def __init__(self, db, rules: RuleSet, sender: Sender, log: Log, *,
                 cooldown: float = 5.0, max_per_minute: int = 6,
                 max_age: float = 180.0, ignore=(), service_prefixes=(),
                 service_suffixes=(), mention_names=(), echo_self: bool = False,
                 ai_fn=None, memory=None, max_reply_chars: int = 2000):
        self.db = db
        self.rules = rules
        self.sender = sender
        self.log = log
        self.cooldown = cooldown
        self.max_per_minute = max_per_minute
        self.max_age = max_age
        self.ai_fn = ai_fn
        self.memory = memory
        self.max_reply_chars = max(0, int(max_reply_chars))
        self.own_wxid = (db.get_self_info() or {}).get("username") or db.wxid or ""
        self.own_names = set(mention_names) | {self.own_wxid}
        try:
            own_nick = db.get_nickname(self.own_wxid)
            if own_nick:
                self.own_names.add(own_nick)
        except Exception:
            pass
        self.ignore = set(ignore) | set(DEFAULT_IGNORE)
        self.service_prefixes = tuple(service_prefixes) + DEFAULT_SERVICE_PREFIXES
        self.service_suffixes = tuple(service_suffixes) + DEFAULT_SERVICE_SUFFIXES
        self._last_reply: Dict[str, float] = {}
        self._recent: Dict[str, deque] = {}
        self._seen: deque = deque(maxlen=4000)
        self._seen_set = set()
        self._lock = threading.Lock()

    # -- 过滤 ----------------------------------------------------------
    def _is_service(self, username: str) -> bool:
        if username in self.ignore:
            return True
        if username.startswith(self.service_prefixes):
            return True
        return username.endswith(self.service_suffixes)

    def _dedup(self, msg: dict) -> bool:
        key = (msg.get("username"), msg.get("shard") or "", msg.get("local_id"))
        with self._lock:
            if key in self._seen_set:
                return True
            self._seen.append(key)
            self._seen_set.add(key)
            while len(self._seen) > self._seen.maxlen:
                old = self._seen.popleft()
                self._seen_set.discard(old)
        return False

    def _rate_limited(self, chat: str) -> bool:
        now = time.time()
        if now - self._last_reply.get(chat, 0.0) < self.cooldown:
            return True
        q = self._recent.setdefault(chat, deque(maxlen=self.max_per_minute + 1))
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= self.max_per_minute:
            return True
        q.append(now)
        self._last_reply[chat] = now
        return False

    def _mentioned(self, text: str) -> bool:
        if self.rules.group_mention_any:
            return "@" in text
        return any("@" + name in text for name in self.own_names if name)

    # -- 回调 ----------------------------------------------------------
    def on_msg(self, msg: dict, _listener=None):
        try:
            self.handle(msg)
        except Exception as exc:  # 回调抛错会被 Listener 重试，这里自己兜住
            self.log("处理消息异常：%s: %s" % (type(exc).__name__, exc))

    def handle(self, msg: dict):
        chat = msg.get("username") or ""
        mtype = msg.get("type")
        if mtype != "文本":
            return
        if self._is_service(chat):
            return
        sender_wxid = (msg.get("sender_username") or "").strip()
        if not sender_wxid or sender_wxid == self.own_wxid:
            return  # 自己发的（含多端同步）不回复
        if self._dedup(msg):
            return
        created = float(msg.get("create_time") or 0)
        if self.max_age > 0 and created and time.time() - created > self.max_age:
            return  # 离线补跑/积压的旧消息不回，避免一启动就刷屏

        is_group = chat.endswith("@chatroom")
        text = clean_text(msg.get("content") or "", is_group)
        if not text.strip():
            return

        if is_group and self.rules.group_prefix and text.lstrip().startswith(self.rules.group_prefix):
            text_for_match = text.lstrip()[len(self.rules.group_prefix):].strip()
        else:
            if is_group and self.rules.group_require_mention and not self._mentioned(text):
                return
            text_for_match = text.strip()

        sender_name = self._sender_name(sender_wxid)
        chat_name = display_name_of(self.db, chat)

        # 开始新对话：只清当前会话记忆，不调模型；回复也不写回记忆。
        if is_reset_command(text_for_match):
            if self.memory:
                self.memory.clear(chat)
                self.log("已清空会话记忆：%s" % chat_name)
                msg = "好的，已开始新对话。"
            else:
                self.log("收到重置命令，但记忆未启用：%s" % chat_name)
                msg = "记忆未启用。"
            self.sender.submit(chat, chat_name, msg)
            return

        matched = self.rules.match(text_for_match, chat)
        use_ai = matched is None and self.ai_fn is not None
        if not matched and not use_ai and not self.rules.default_reply:
            return
        # 频率限制放在调用 AI 之前，避免冷却期内白花 token
        if self._rate_limited(chat):
            self.log("跳过（冷却/频率限制）：%s" % chat_name)
            return

        reply = matched
        if reply is None and use_ai:
            history = self.memory.get(chat) if self.memory else None
            # 群里多人共用一个会话记忆，正文带上发送者，模型才分得清谁说的
            user_line = ("%s：%s" % (sender_name, text_for_match)
                         if is_group else text_for_match)
            ai_text = self.ai_fn(user_line, sender_name, chat_name, history)
            if ai_text:
                ai_text = self._cap(ai_text)
                if self.memory:
                    self.memory.add(chat, "user", user_line)
                    self.memory.add(chat, "assistant", ai_text)
                self.log("AI 回复：%s | %s: %s -> %s"
                         % (chat_name, sender_name, text_for_match[:40], ai_text[:60]))
                self.sender.submit(chat, chat_name, ai_text)
                return
            self.log("AI 没返回内容，改用兜底：%s" % chat_name)

        if reply is None:
            reply = self.rules.default_reply
        if not reply:
            return
        reply = render_template(reply, sender_name, chat_name, text_for_match)
        reply = self._cap(reply)
        self.log("命中：%s | %s: %s -> %s"
                 % (chat_name, sender_name, text_for_match[:40], reply[:60]))
        self.sender.submit(chat, chat_name, reply)

    def _cap(self, text: str) -> str:
        if self.max_reply_chars > 0 and len(text) > self.max_reply_chars:
            self.log("回复超长（%d 字），截断到 %d 字"
                     % (len(text), self.max_reply_chars))
            return cap_text(text, self.max_reply_chars)
        return text

    def _sender_name(self, wxid: str) -> str:
        try:
            return self.db.nickname_map().get(wxid) or wxid
        except Exception:
            return wxid


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------
def self_test() -> int:
    rules = RuleSet(rules=[
        Rule(match=["你好", "hi"], reply="你好呀，我是自动回复机器人。"),
        Rule(match=["几点", "时间"], reply="现在是 {time}。"),
        Rule(match=[r"^/help$"], reply="可用指令：/help", regex=True),
    ], default_reply="收到，稍后回复你。")
    cases = [
        ("你好", False, "你好呀，我是自动回复机器人。"),
        ("Hi there", False, "你好呀，我是自动回复机器人。"),
        ("现在几点？", False, "现在是 {time}。"),
        ("/help", False, "可用指令：/help"),
        ("随便说点什么", False, "收到，稍后回复你。"),
        # 群聊正文带发送者前缀：自定义微信号（不是 wxid_ 开头）也要剥掉
        ("z84772180:\n你好", True, "你好呀，我是自动回复机器人。"),
        ("wxid_abc:\n你好", True, "你好呀，我是自动回复机器人。"),
    ]
    bad = 0

    def decide(text: str) -> str:
        return rules.match(clean_text(text, False), "wxid_x") or rules.default_reply

    for text, is_group, want in cases:
        got = decide(clean_text(text, is_group))
        if got != want:
            bad += 1
            print("FAIL", repr(text), "->", repr(got), "want", repr(want))
        else:
            print("ok  ", repr(text), "->", repr(got))
    print("self-test:", "PASS" if not bad else "%d FAILED" % bad)

    # 重置命令识别
    for cmd in ("/new", "/reset", "/重置对话", "重置对话", "开始新对话"):
        if not is_reset_command(cmd):
            bad += 1
            print("FAIL reset command:", repr(cmd))
    for cmd in ("/help", "新的对话", "重置"):
        if is_reset_command(cmd):
            bad += 1
            print("FAIL reset command should not match:", repr(cmd))
    # 长度截断
    capped = cap_text("一" * 10, 5)
    if capped != "一一一一…" or len(capped) != 5:
        bad += 1
        print("FAIL cap_text:", repr(capped))
    if cap_text("短", 2000) != "短":
        bad += 1
        print("FAIL cap_text short")
    print("self-test:", "PASS" if not bad else "%d FAILED" % bad)
    return 0 if not bad else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m wechatauto.demo_auto_reply",
        description="微信自动回复（数据库监听 + 规则回复）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--chat", action="append", default=[],
                   help="要监听的会话（昵称/备注/群名/wxid，可重复）")
    p.add_argument("--all", action="store_true", help="监听全部会话（含运行中新出现的）")
    p.add_argument("--rules", default="", help="规则 JSON 文件（见 auto_reply_rules.example.json）")
    p.add_argument("--default-reply", default="", help="没有命中任何规则时的兜底回复（空=不回）")
    p.add_argument("--group-prefix", default=None, help="群聊触发前缀（默认 !，传空串关闭）")
    p.add_argument("--mention-any", action="store_true", help="群里任意 @ 都触发（默认只认 @自己）")
    p.add_argument("--no-mention-only", action="store_true", help="群里不要求 @（慎用，容易刷屏）")
    p.add_argument("--interval", type=float, default=1.0, help="数据库轮询间隔（秒）")
    p.add_argument("--cooldown", type=float, default=5.0, help="同一会话两次回复的最小间隔（秒）")
    p.add_argument("--max-per-minute", type=int, default=6, help="同一会话每分钟最多回复条数")
    p.add_argument("--max-age", type=float, default=180.0,
                   help="超过这个秒数的旧消息不回（0=不限）")
    p.add_argument("--dry-run", action="store_true", help="只打印将回复的内容，不点微信窗口")
    p.add_argument("--ai", action="store_true",
                   help="调用 OpenAI 兼容接口（未命中规则时；没有规则就是纯 AI）")
    p.add_argument("--ocr", action="store_true",
                   help="允许 OCR 兜底（默认关闭：UIA 失败时宁可报错，也不在界面上盲点）")
    p.add_argument("--memory-turns", type=int, default=10,
                   help="每个会话保留的对话轮数（0=关闭记忆）")
    p.add_argument("--memory-chars", type=int, default=3000,
                   help="每个会话记忆的总字符上限")
    p.add_argument("--memory-file", default="",
                   help="记忆文件路径（默认 memory/auto_reply_<账号>.json）")
    p.add_argument("--no-memory", action="store_true", help="关闭会话记忆")
    p.add_argument("--max-reply-chars", type=int, default=2000,
                   help="单条回复长度上限（字，0=不限）")
    p.add_argument("--rhythm", choices=["calm", "natural", "fast"], default="calm",
                   help="拟人节奏档位（默认 calm：6-14s 间隔 / 300s 内 3 次）")
    p.add_argument("--rhythm-gap", nargs=2, type=float, metavar=("MIN", "MAX"),
                   default=None, help="覆盖写动作最小间隔（秒）")
    p.add_argument("--rhythm-burst", type=int, default=None,
                   help="覆盖窗口内写动作上限")
    p.add_argument("--rhythm-window", type=float, default=None,
                   help="覆盖突发统计窗口（秒）")
    p.add_argument("--rhythm-cooloff", nargs=2, type=float, metavar=("MIN", "MAX"),
                   default=None, help="覆盖触发上限后的冷却（秒）")
    p.add_argument("--verify", dest="verify", action="store_true",
                   help="发送后回读数据库确认（默认关闭：微信落库可能晚于 8 秒，会误报）")
    p.add_argument("--no-verify", dest="verify", action="store_false",
                   help=argparse.SUPPRESS)
    p.add_argument("--ignore", action="append", default=[], help="忽略的会话 username（可重复）")
    p.add_argument("--mention-name", action="append", default=[],
                   help="群里 @ 自己的别名（默认自动加本机昵称，可重复）")
    p.add_argument("--account", default="", help="账号目录名（多开时必填，见 list_accounts）")
    p.add_argument("--hwnd", type=int, default=0, help="强制指定微信主窗口句柄（默认自动找）")
    p.add_argument("--log-file", default="", help="把日志同时写入该文件")
    p.add_argument("--self-test", action="store_true", help="只跑规则自检，不连微信")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.self_test:
        return self_test()

    log = Log(args.log_file)
    # 自动回复默认关闭 OCR 兜底：UIA 打不开会话时宁可失败，也不要在界面上盲点
    # （盲点可能最小化几十个窗口，甚至点到退出登录）。
    from wechatauto.param import WxParam
    WxParam.ENABLE_OCR = bool(args.ocr)
    log("OCR 兜底：%s" % ("开启" if args.ocr else "关闭（默认，避免盲点）"))

    # 拟人节奏：档位 + 可选的单项覆盖（GUI 里也是这几个数）
    from wechatauto import rhythm as rhythm_mod
    rhythm_mod.set_profile(args.rhythm)
    over = {}
    if args.rhythm_gap:
        over["gap"] = tuple(args.rhythm_gap)
    if args.rhythm_burst is not None:
        over["burst"] = max(0, int(args.rhythm_burst))
    if args.rhythm_window is not None:
        over["window"] = max(10.0, float(args.rhythm_window))
    if args.rhythm_cooloff:
        over["cooloff"] = tuple(args.rhythm_cooloff)
    if over:
        rhythm_mod.configure(**over)
    p = rhythm_mod.profile()
    log("拟人节奏：%s | 间隔 %.1f-%.1fs | %.0fs 内 %d 次 | 冷却 %.0f-%.0fs"
        % (p.name, p.gap[0], p.gap[1], p.window, p.burst,
           p.cooloff[0], p.cooloff[1]))
    log("单条回复上限：%d 字" % args.max_reply_chars)

    # 规则
    rules = RuleSet.from_json(args.rules) if args.rules else RuleSet()
    if args.default_reply:
        rules.default_reply = args.default_reply
    if args.group_prefix is not None:
        rules.group_prefix = args.group_prefix
    if args.mention_any:
        rules.group_mention_any = True
    if args.no_mention_only:
        rules.group_require_mention = False
    if not rules.rules and not rules.default_reply and not args.ai:
        log("没有可回复内容：用 --rules / --default-reply，或加 --ai 走纯 AI。")
        return 2

    # 监听目标
    from wechatauto import WeChatDB
    from wechatauto.db import Listener

    db = WeChatDB(account=args.account) if args.account else WeChatDB()
    info = db.get_self_info()
    log("账号：%s（%s）" % (info.get("nick_name") or info.get("username"), db.wxid))

    memory = None
    if not args.no_memory and args.memory_turns > 0:
        from wechatauto.chat_memory import ChatMemory
        mem_path = args.memory_file or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "memory", "auto_reply_%s.json" % db.account)
        memory = ChatMemory(mem_path, max_turns=args.memory_turns,
                            max_chars=args.memory_chars)
        log("会话记忆：每会话 %d 轮 / %d 字（%s）"
            % (args.memory_turns, args.memory_chars, mem_path))
        log("发送 /new 或 重置对话 可清空当前会话记忆")
    else:
        log("会话记忆：关闭")

    targets: List[str] = []
    if args.all:
        targets = []
    else:
        if not args.chat:
            log("请用 --chat 指定要监听的会话（可重复），或 --all 监听全部。"
                "先跑 --self-test 看规则格式。")
            return 2
        for raw in args.chat:
            got = resolve_chat(db, raw)
            if not got:
                log("找不到会话：%s（先用 wechatauto sessions 看 username/昵称）" % raw)
                return 2
            targets.append(got)

    hwnd = args.hwnd or (find_hwnd_for_account(args.account)
                         if args.account else find_wechat_hwnd())
    if not hwnd and not args.dry_run:
        log("找不到微信主窗口：微信是否已启动并登录？"
            "（脚本只认 Weixin.exe 的主窗口，不会误绑标题带“微信”的其它程序）")
        return 3
    if not hwnd:
        log("dry-run：未找到微信主窗口，仍只做规则匹配（不会发送）")

    sender = Sender(hwnd, log, verify=args.verify, dry_run=args.dry_run)
    sender.start()

    ai_fn = None
    if args.ai:
        from wechatauto.ai_reply import make_reply_fn
        ai_fn = make_reply_fn(max_chars=args.max_reply_chars)
        if ai_fn is None:
            log("已勾选 --ai，但没读到 WECHATAUTO_AI_KEY，本次不使用 AI")
        else:
            log("AI 已启用：规则未命中时调用模型")

    replier = AutoReplier(
        db, rules, sender, log,
        cooldown=max(0.0, args.cooldown),
        max_per_minute=max(1, args.max_per_minute),
        max_age=max(0.0, args.max_age),
        ignore=args.ignore,
        mention_names=args.mention_name,
        ai_fn=ai_fn,
        memory=memory,
        max_reply_chars=args.max_reply_chars,
    )

    listener = Listener(db, interval=max(0.2, args.interval))
    if args.all:
        listener.add_all(replier.on_msg, discover=True)
        log("监听范围：全部会话（运行中新出现的会话自动纳入）")
    else:
        for u in targets:
            listener.add_listener(u, replier.on_msg)
            log("监听：%s（%s）" % (display_name_of(db, u), u))

    log("模式：%s | 群聊要求 @：%s | 群触发前缀：%r | 冷却 %.0fs / 每分钟 %d 条"
        % ("dry-run（不发送）" if args.dry_run else "真实发送",
           rules.group_require_mention, rules.group_prefix,
           args.cooldown, args.max_per_minute))
    log("开始监听（Ctrl+C 停止）")
    listener.start()
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        log("收到 Ctrl+C，停止中…")
    finally:
        listener.stop()
        sender.stop(release=not args.dry_run)
        log("已停止。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
