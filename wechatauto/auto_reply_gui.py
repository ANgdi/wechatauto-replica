# -*- coding: utf-8 -*-
"""自动回复图形界面（Tkinter）——给不想敲命令的场景用。

启动::

    python -m wechatauto.auto_reply_gui

界面里选账号、填会话（留空=全部）、选规则文件，先「干跑」确认命中，
再取消干跑去掉「干跑」勾选即可真实发送。关闭窗口会停止机器人。
"""
from __future__ import annotations

import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

try:
    import ctypes
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass


def _default_rules_path() -> str:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = os.path.join(root, "auto_reply_rules.example.json")
    return p if os.path.exists(p) else ""


class AutoReplyApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.log_q: "queue.Queue[str]" = queue.Queue()
        self.stop_event = threading.Event()
        self.listener = None
        self.sender = None
        self.worker = None
        self.accounts = {}

        root.title("微信自动回复")
        root.geometry("960x900")
        root.minsize(820, 760)
        try:
            root.option_add("*Font", ("Microsoft YaHei UI", 10))
        except Exception:
            pass

        self._build()
        self._refresh_accounts()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(120, self._bring_to_front)
        root.after(200, self._drain)

    def _bring_to_front(self):
        """从终端启动时窗口可能生在其他窗口后面，主动置前一次。"""
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.attributes("-topmost", True)
            self.root.focus_force()
            self.root.after(900, lambda: self.root.attributes("-topmost", False))
        except Exception:
            pass

    # -- UI ------------------------------------------------------------
    def _build(self):
        pad = {"padx": 8, "pady": 4}

        top = ttk.LabelFrame(self.root, text="账号与监听范围")
        top.pack(fill="x", **pad)
        top.columnconfigure(1, weight=1, minsize=260)

        ttk.Label(top, text="账号：").grid(row=0, column=0, sticky="w", padx=6, pady=6)
        self.account_var = tk.StringVar()
        self.account_box = ttk.Combobox(top, textvariable=self.account_var,
                                        state="readonly", width=22)
        self.account_box.grid(row=0, column=1, sticky="we")
        ttk.Button(top, text="刷新", command=self._refresh_accounts).grid(row=0, column=2, padx=6)
        ttk.Button(top, text="最近会话", command=self._peek_sessions).grid(row=0, column=3, padx=6)

        ttk.Label(top, text="会话：").grid(row=1, column=0, sticky="w", padx=6, pady=6)
        self.chats_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.chats_var).grid(row=1, column=1, columnspan=2, sticky="we")
        ttk.Label(top, text="留空=全部，逗号分隔").grid(row=1, column=3, sticky="w")

        cfg = ttk.LabelFrame(self.root, text="回复规则")
        cfg.pack(fill="x", **pad)
        cfg.columnconfigure(1, weight=1)

        ttk.Label(cfg, text="规则文件：").grid(row=0, column=0, sticky="w", padx=6, pady=6)
        self.rules_var = tk.StringVar(value="")
        ttk.Entry(cfg, textvariable=self.rules_var).grid(row=0, column=1, sticky="we")
        ttk.Button(cfg, text="浏览…", command=self._pick_rules).grid(row=0, column=2, padx=6)

        ttk.Label(cfg, text="兜底回复：").grid(row=1, column=0, sticky="w", padx=6, pady=6)
        self.default_var = tk.StringVar()
        ttk.Entry(cfg, textvariable=self.default_var).grid(row=1, column=1, sticky="we")
        ttk.Label(cfg, text="留空=未命中不回").grid(row=1, column=2, sticky="w", padx=6)

        opts = ttk.Frame(self.root)
        opts.pack(fill="x", **pad)
        row1 = ttk.Frame(opts); row1.pack(fill="x")
        row2 = ttk.Frame(opts); row2.pack(fill="x", pady=(4, 0))
        self.dry_var = tk.BooleanVar(value=True)
        self.verify_var = tk.BooleanVar(value=False)
        self.mention_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(row1, text="干跑（不发送）", variable=self.dry_var).pack(side="left", padx=6)
        ttk.Checkbutton(row1, text="回读确认（可能误报）", variable=self.verify_var).pack(side="left", padx=6)
        ttk.Checkbutton(row1, text="群里@我", variable=self.mention_var).pack(side="left", padx=6)
        ttk.Label(row2, text="群触发前缀：").pack(side="left", padx=(6, 2))
        self.prefix_var = tk.StringVar(value="!")
        ttk.Entry(row2, textvariable=self.prefix_var, width=4).pack(side="left")
        ttk.Label(row2, text="同会话冷却(秒)：").pack(side="left", padx=(16, 2))
        self.cool_var = tk.DoubleVar(value=5.0)
        ttk.Spinbox(row2, from_=0, to=600, increment=1, textvariable=self.cool_var, width=5).pack(side="left")
        ttk.Label(row2, text="每分钟上限：").pack(side="left", padx=(16, 2))
        self.permin_var = tk.IntVar(value=6)
        ttk.Spinbox(row2, from_=1, to=60, textvariable=self.permin_var, width=4).pack(side="left")

        row3 = ttk.Frame(opts)
        row3.pack(fill="x", pady=(4, 0))
        ttk.Label(row3, text="额外@别名：").pack(side="left", padx=(6, 2))
        self.mention_names_var = tk.StringVar()
        ttk.Entry(row3, textvariable=self.mention_names_var, width=16).pack(side="left")
        ttk.Label(row3, foreground="#666", text="（逗号分隔）").pack(side="left", padx=(8, 0))

        row4 = ttk.Frame(opts)
        row4.pack(fill="x", pady=(2, 0))
        ttk.Label(row4, foreground="#666",
                  text="群聊：「!」开头或 @我/别名；/new 或 重置对话 清空记忆。"
                  ).pack(side="left", padx=6)

        row5 = ttk.Frame(opts)
        row5.pack(fill="x", pady=(2, 0))
        self.ai_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(row5, text="用 AI 回复（无规则=纯 AI）",
                        variable=self.ai_var).pack(side="left", padx=6)
        ttk.Label(row5, foreground="#666",
                  text=".env 里配 WECHATAUTO_AI_KEY"
                  ).pack(side="left", padx=(4, 0))

        limits = ttk.LabelFrame(self.root, text="节奏与限制")
        limits.pack(fill="x", **pad)

        l1 = ttk.Frame(limits)
        l1.pack(fill="x", padx=6, pady=(6, 2))
        ttk.Label(l1, text="节奏：").pack(side="left")
        self.rhythm_var = tk.StringVar(value="calm")
        ttk.Combobox(l1, textvariable=self.rhythm_var, state="readonly", width=8,
                     values=["calm", "natural", "fast"]).pack(side="left")
        ttk.Label(l1, text="   写间隔：").pack(side="left")
        self.gap_lo_var = tk.DoubleVar(value=6.0)
        self.gap_hi_var = tk.DoubleVar(value=14.0)
        ttk.Spinbox(l1, from_=0, to=600, increment=1, textvariable=self.gap_lo_var,
                    width=4).pack(side="left")
        ttk.Label(l1, text="~").pack(side="left", padx=2)
        ttk.Spinbox(l1, from_=0, to=600, increment=1, textvariable=self.gap_hi_var,
                    width=4).pack(side="left")
        ttk.Label(l1, text="秒   窗口：").pack(side="left")
        self.window_var = tk.DoubleVar(value=300.0)
        ttk.Spinbox(l1, from_=10, to=3600, increment=10, textvariable=self.window_var,
                    width=5).pack(side="left")
        ttk.Label(l1, text="秒").pack(side="left")

        l2 = ttk.Frame(limits)
        l2.pack(fill="x", padx=6, pady=2)
        ttk.Label(l2, text="窗口内次数：").pack(side="left")
        self.burst_var = tk.IntVar(value=3)
        ttk.Spinbox(l2, from_=0, to=100, increment=1, textvariable=self.burst_var,
                    width=4).pack(side="left")
        ttk.Label(l2, text="   触发后冷却：").pack(side="left")
        self.cool_lo_var = tk.DoubleVar(value=60.0)
        self.cool_hi_var = tk.DoubleVar(value=150.0)
        ttk.Spinbox(l2, from_=0, to=3600, increment=5, textvariable=self.cool_lo_var,
                    width=4).pack(side="left")
        ttk.Label(l2, text="~").pack(side="left", padx=2)
        ttk.Spinbox(l2, from_=0, to=3600, increment=5, textvariable=self.cool_hi_var,
                    width=4).pack(side="left")
        ttk.Label(l2, text="秒").pack(side="left")

        l3 = ttk.Frame(limits)
        l3.pack(fill="x", padx=6, pady=(2, 6))
        ttk.Label(l3, text="记忆轮数：").pack(side="left")
        self.memory_turns_var = tk.IntVar(value=10)
        ttk.Spinbox(l3, from_=0, to=50, increment=1, textvariable=self.memory_turns_var,
                    width=4).pack(side="left")
        ttk.Label(l3, text="   记忆字数：").pack(side="left")
        self.memory_chars_var = tk.IntVar(value=3000)
        ttk.Spinbox(l3, from_=200, to=20000, increment=200,
                    textvariable=self.memory_chars_var, width=6).pack(side="left")
        ttk.Label(l3, text="   单条上限：").pack(side="left")
        self.max_reply_var = tk.IntVar(value=2000)
        ttk.Spinbox(l3, from_=50, to=5000, increment=100,
                    textvariable=self.max_reply_var, width=6).pack(side="left")
        ttk.Label(l3, text="字").pack(side="left")

        ctrl = ttk.Frame(self.root)
        ctrl.pack(fill="x", **pad)
        self.btn_start = ttk.Button(ctrl, text="启动", command=self._start)
        self.btn_start.pack(side="left", padx=6)
        self.btn_stop = ttk.Button(ctrl, text="停止", command=self._stop, state="disabled")
        self.btn_stop.pack(side="left", padx=6)
        self.status_var = tk.StringVar(value="未启动")
        ttk.Label(ctrl, textvariable=self.status_var, foreground="#555").pack(side="left", padx=12)

        logbox = ttk.LabelFrame(self.root, text="运行日志")
        logbox.pack(fill="both", expand=True, **pad)
        self.log_widget = scrolledtext.ScrolledText(logbox, height=18, state="disabled", wrap="word")
        self.log_widget.pack(fill="both", expand=True, padx=6, pady=6)

        self.root.report_callback_exception = self._on_tk_error

    def _on_tk_error(self, exc, val, tb):
        self._log("界面异常：%s: %s" % (exc, val))

    def _pick_rules(self):
        p = filedialog.askopenfilename(
            title="选择规则 JSON", filetypes=[("JSON", "*.json"), ("所有文件", "*.*")])
        if p:
            self.rules_var.set(p)

    # -- 账号 ------------------------------------------------------------
    def _refresh_accounts(self):
        try:
            from wechatauto.db import list_accounts
            rows = list_accounts()
        except Exception as e:
            self._log("读取账号列表失败：%s" % e)
            return
        self.accounts = {}
        for a in rows:
            key = a.get("wxid") or a["account"]
            if key in self.accounts:          # 极少数 wxid 相同的情况，带上目录区分
                key = "%s (%s)" % (key, a["account"])
            self.accounts[key] = a["account"]
        self.account_box["values"] = list(self.accounts)
        if self.accounts and not self.account_var.get():
            self.account_box.current(0)
        self._log("账号：%s" % ("、".join(self.accounts) or "无"))

    # -- 启动/停止 --------------------------------------------------------
    def _start(self):
        if self.worker is not None and self.worker.is_alive():
            return
        account = self.accounts.get(self.account_var.get())
        if not account:
            messagebox.showwarning("提示", "请先选择账号")
            return
        rules_path = self.rules_var.get().strip()
        default = self.default_var.get().strip()
        if not rules_path and not default and not self.ai_var.get():
            messagebox.showwarning("提示", "规则文件、兜底回复、AI 至少开一个，否则没有可回的内容")
            return
        if rules_path and not os.path.exists(rules_path):
            messagebox.showwarning("提示", "规则文件不存在：\n%s" % rules_path)
            return
        cfg = {
            "account": account,
            "chats": self.chats_var.get().strip(),
            "rules": rules_path,
            "default": default,
            "dry": bool(self.dry_var.get()),
            "verify": bool(self.verify_var.get()),
            "mention": bool(self.mention_var.get()),
            "mention_names": [x.strip() for x in
                              self.mention_names_var.get().replace("，", ",").split(",")
                              if x.strip()],
            "ai": bool(self.ai_var.get()),
            "memory_turns": max(0, int(self.memory_turns_var.get())),
            "memory_chars": max(200, int(self.memory_chars_var.get())),
            "max_reply_chars": max(0, int(self.max_reply_var.get())),
            "rhythm": self.rhythm_var.get(),
            "gap": (max(0.0, float(self.gap_lo_var.get())),
                    max(0.0, float(self.gap_hi_var.get()))),
            "window": max(10.0, float(self.window_var.get())),
            "burst": max(0, int(self.burst_var.get())),
            "cooloff": (max(0.0, float(self.cool_lo_var.get())),
                        max(0.0, float(self.cool_hi_var.get()))),
            "prefix": self.prefix_var.get(),
            "cooldown": max(0.0, float(self.cool_var.get())),
            "permin": max(1, int(self.permin_var.get())),
        }
        self.stop_event.clear()
        self.listener = self.sender = None
        self.worker = threading.Thread(target=self._run_bot, args=(cfg,), daemon=True)
        self.worker.start()
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.status_var.set("运行中（%s）" % ("干跑" if cfg["dry"] else "真实发送"))

    def _stop(self):
        if self.worker is None or not self.worker.is_alive():
            self.btn_stop.config(state="disabled")
            self.btn_start.config(state="normal")
            self.status_var.set("已停止")
            return
        self.status_var.set("停止中…")
        self.stop_event.set()
        self.btn_stop.config(state="disabled")

    def _run_bot(self, cfg):
        from wechatauto.db import Listener, WeChatDB
        from wechatauto.demo_auto_reply import (AutoReplier, RuleSet, Sender,
                                                display_name_of,
                                                find_hwnd_for_account,
                                                resolve_chat)
        listener = sender = None
        try:
            # 自动回复默认不让 OCR 兜底：UIA 打不开会话时宁可失败，
            # 也不要盲点界面（盲点可能最小化一堆窗口，甚至点到退出登录）。
            from wechatauto.param import WxParam
            WxParam.ENABLE_OCR = False
            self._log("OCR 兜底：关闭（UIA 打不开会话时会报错，不会盲点）")

            from wechatauto import rhythm as rhythm_mod
            rhythm_mod.set_profile(cfg["rhythm"])
            rhythm_mod.configure(gap=tuple(cfg["gap"]), burst=cfg["burst"],
                                 window=cfg["window"],
                                 cooloff=tuple(cfg["cooloff"]))
            rp = rhythm_mod.profile()
            self._log("节奏：%s | 间隔 %.1f–%.1fs | %.0fs 内 %d 次 | 冷却 %.0f–%.0fs"
                      % (rp.name, rp.gap[0], rp.gap[1], rp.window, rp.burst,
                         rp.cooloff[0], rp.cooloff[1]))
            self._log("单条回复上限：%d 字" % cfg["max_reply_chars"])
            self._log("加载账号数据库（首次可能需要 10–60 秒）…")
            db = WeChatDB(account=cfg["account"])
            info = db.get_self_info()
            self._log("账号：%s（%s）" % (info.get("nick_name") or "", db.wxid))

            rules = RuleSet.from_json(cfg["rules"]) if cfg["rules"] else RuleSet()
            if cfg["default"]:
                rules.default_reply = cfg["default"]
            rules.group_require_mention = cfg["mention"]
            if cfg["prefix"] is not None:
                rules.group_prefix = cfg["prefix"]

            hwnd = find_hwnd_for_account(cfg["account"])
            if hwnd:
                self._log("微信窗口 hwnd=%s" % hwnd)
            elif cfg["dry"]:
                self._log("干跑：未找到该账号窗口，只做读库匹配")
            else:
                self._log("找不到该账号的微信窗口，已停止（请确认微信已登录）")
                return

            sender = Sender(hwnd, self._log, verify=cfg["verify"], dry_run=cfg["dry"])
            sender.start()
            ai_fn = None
            if cfg["ai"]:
                from wechatauto.ai_reply import make_reply_fn
                ai_fn = make_reply_fn(max_chars=cfg["max_reply_chars"])
                if ai_fn is None:
                    self._log("已勾选 AI，但没读到 WECHATAUTO_AI_KEY，本次不使用 AI")
                else:
                    self._log("AI 已启用：没命中规则时调用模型")
            memory = None
            if cfg["memory_turns"] > 0:
                from wechatauto.chat_memory import ChatMemory
                mem_path = os.path.join(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "memory", "auto_reply_%s.json" % cfg["account"])
                memory = ChatMemory(mem_path, max_turns=cfg["memory_turns"],
                                    max_chars=cfg["memory_chars"])
                self._log("会话记忆：每会话 %d 轮 / %d 字"
                          % (cfg["memory_turns"], cfg["memory_chars"]))
                self._log("发送 /new 或 重置对话 可清空当前会话记忆")
            else:
                self._log("会话记忆：关闭")
            replier = AutoReplier(db, rules, sender, self._log,
                                  cooldown=cfg["cooldown"],
                                  max_per_minute=cfg["permin"],
                                  mention_names=cfg["mention_names"],
                                  ai_fn=ai_fn, memory=memory,
                                  max_reply_chars=cfg["max_reply_chars"])
            listener = Listener(db, interval=1.0)

            raw = [x.strip() for x in cfg["chats"].replace("，", ",").split(",") if x.strip()]
            if not raw:
                listener.add_all(replier.on_msg, discover=True)
                self._log("监听：全部会话（运行中新出现的也纳入）")
            else:
                for name in raw:
                    u = resolve_chat(db, name)
                    if not u:
                        self._log("找不到会话：%s（跳过）" % name)
                        continue
                    listener.add_listener(u, replier.on_msg)
                    self._log("监听：%s（%s）" % (display_name_of(db, u), u))
            listener.start()
            self._log("已启动。点「停止」结束。")
            self.stop_event.wait()
        except Exception as e:
            self._log("运行失败：%s: %s" % (type(e).__name__, e))
        finally:
            try:
                if listener is not None:
                    listener.stop()
            except Exception:
                pass
            try:
                if sender is not None:
                    sender.stop(release=not cfg.get("dry"))
            except Exception:
                pass
            self._log("已停止。")

    def _peek_sessions(self):
        account = self.accounts.get(self.account_var.get())
        if not account:
            messagebox.showwarning("提示", "请先选择账号")
            return

        def work():
            try:
                from wechatauto.db import WeChatDB
                self._log("读取 %s 的会话列表…" % account)
                db = WeChatDB(account=account)
                for s in db.get_sessions(limit=20):
                    self._log("  %-24s 未读 %-3s %s" % (
                        db.get_nickname(s["username"])[:22], s["unread"],
                        (s["summary"] or "")[:36]))
            except Exception as e:
                self._log("读取会话失败：%s: %s" % (type(e).__name__, e))

        threading.Thread(target=work, daemon=True).start()

    # -- 日志 ------------------------------------------------------------
    def _log(self, msg: str):
        self.log_q.put("[%s] %s" % (time.strftime("%H:%M:%S"), msg))

    def _drain(self):
        try:
            while True:
                line = self.log_q.get_nowait()
                self.log_widget.config(state="normal")
                self.log_widget.insert("end", line + "\n")
                self.log_widget.see("end")
                self.log_widget.config(state="disabled")
        except queue.Empty:
            pass
        if self.worker is not None and not self.worker.is_alive():
            self.worker = None
            self.btn_start.config(state="normal")
            self.btn_stop.config(state="disabled")
            self.status_var.set("已停止")
        self.root.after(200, self._drain)

    def _on_close(self):
        running = self.worker is not None and self.worker.is_alive()
        if running and not messagebox.askyesno("退出", "自动回复正在运行，确定退出吗？"):
            return
        self.stop_event.set()
        self.root.after(400, self.root.destroy)


def main() -> int:
    root = tk.Tk()
    app = AutoReplyApp(root)  # noqa: F841
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
