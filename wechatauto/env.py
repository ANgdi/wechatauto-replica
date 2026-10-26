# -*- coding: utf-8 -*-
"""极简 .env 加载器（不引入 python-dotenv）。

规则：

- 只读取 ``KEY=VALUE``，忽略空行与 ``#`` 注释；
- 支持 ``export KEY=VALUE``、单引号/双引号；
- **不覆盖已存在的环境变量**（系统环境优先于 .env）；
- 默认查找 当前目录/.env 和 仓库根目录/.env。

用法::

    from wechatauto.env import load_dotenv
    load_dotenv()          # 只补缺失的变量
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Iterable, Optional


def _candidates() -> Iterable[Path]:
    seen = set()
    for p in (Path.cwd() / ".env",
              Path(__file__).resolve().parent.parent / ".env"):
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            yield rp


def parse_env_text(text: str) -> Dict[str, str]:
    """把 .env 文本解析成 dict（不写环境变量）。"""
    out: Dict[str, str] = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        out[key] = val
    return out


def load_dotenv(paths: Optional[Iterable[str]] = None,
                override: bool = False) -> Dict[str, str]:
    """加载 .env 到 os.environ，返回本次真正写入的键值。

    ``override=False`` 时**不会覆盖**已有环境变量：系统环境变量优先。
    """
    loaded: Dict[str, str] = {}
    candidates = [Path(p) for p in paths] if paths else list(_candidates())
    for p in candidates:
        try:
            text = p.read_text(encoding="utf-8-sig")
        except OSError:
            continue
        for k, v in parse_env_text(text).items():
            if override or k not in os.environ:
                os.environ[k] = v
                loaded[k] = v
    return loaded
