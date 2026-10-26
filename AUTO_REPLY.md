# 自动回复接入指南（wechatauto-replica）

> 适用：微信 4.x Windows 桌面端，已登录。读消息走本地数据库，回复走 UIA/OCR 驱动真实客户端。
> 本文对应仓库新增的 `wechatauto/demo_auto_reply.py`。

## 1. 环境

```powershell
conda activate localgpt          # Python 3.10（本机验证）
pip install -e ".[guia]"
python -m wechatauto doctor      # 账号 / DB 密钥 / 图片密钥 / 控件树
```

本机安装时踩过两个坑（已修好，重装时注意）：

- 环境里的 `numpy 1.25.2` 元数据损坏 → `pip` 装 `opencv-python` 会要求 numpy 2，
  直接升级会波及其它依赖。做法：修好 numpy 的 dist-info 后装
  `opencv-python==4.9.0.80`（兼容 numpy 1.x）。
- `Pillow` 安装不完整（`from PIL import Image` 报 unknown location）→
  `pip install --force-reinstall --no-deps Pillow==10.0.0`。

## 2. 先干跑，不要一上来就发

```powershell
python -m wechatauto.demo_auto_reply --chat 姚浩宇 --dry-run
```

`--dry-run` 只做数据库监听 + 规则匹配，**不点微信窗口**；命中时打印「将回复 …」。

规则可直接写在 JSON 里（见 `auto_reply_rules.example.json`）：

```json
{
  "group_prefix": "!",
  "group_require_mention": true,
  "default_reply": "",
  "rules": [
    {"match": ["你好", "hi"], "reply": "你好，我是自动回复机器人～"},
    {"match": ["几点", "时间"], "reply": "现在是 {time}。"},
    {"match": ["^/ping$"], "regex": true, "reply": "pong"}
  ]
}
```

`match` 支持子串 / 正则（`regex: true`）/ 整句（`exact: true`）；
`reply` 支持 `{sender}`、`{chat}`、`{content}`、`{time}`。
`chats: ["wxid_xxx"]` 可把一条规则限定到指定会话。

## 2.5 图形界面（不想敲命令就用这个）

![自动回复界面](docs/auto_reply_gui.png)

双击仓库根目录的 **`启动自动回复.bat`**，或者：

```powershell
conda activate localgpt
cd D:\Gitspace\wechatauto-replica
python -m wechatauto.auto_reply_gui
```

界面用法：

1. 账号下拉框选要挂的号（多开时这里会列出多个，选哪个就只读/只发哪个）；
2. 「会话」留空 = 全部会话；只想盯某个群/某个人就填昵称或群名，多个用英文逗号；
3. 规则文件默认已指向 `auto_reply_rules.example.json`，可点「浏览…」换；
4. **先保持「干跑」勾选**，点「启动」，看日志里有没有「命中：… -> …」；
5. 确认规则对了，取消「干跑」再点「启动」——这次才会真正发微信；
6. 点「停止」结束；关窗口也会停。

> 界面只是把 CLI 的参数搬到按钮上，发送链路、节流、回读确认完全一样。

## 3. 真正回复

```powershell
# 监听指定会话（昵称/备注/群名/wxid 都行）
python -m wechatauto.demo_auto_reply --chat 姚浩宇 --rules auto_reply_rules.json

# 多开：指定账号 + 账号自己的窗口（不指定 --hwnd 时按账号目录自动找）
python -m wechatauto.demo_auto_reply --account lee65786_a6d5 `
    --chat 红红火火群 --rules auto_reply_rules.json

# 监听全部会话，未命中给兜底
python -m wechatauto.demo_auto_reply --all --rules auto_reply_rules.json --default-reply "稍后回复你"

# 群里只回 @我 或 "!命令"
python -m wechatauto.demo_auto_reply --all --rules auto_reply_rules.json
```

### 默认行为（安全阀）

| 行为 | 默认 | 说明 |
|---|---|---|
| 只回文本 | ✅ | 图片/语音/文件/表情不触发 |
| 不回自己 | ✅ | `sender_username == 本机 wxid` 直接丢弃（含手机端同步） |
| 不回公众号 | ✅ | `gh_*`、`@openim`、`filehelper` 等 |
| 群聊要求 @ | ✅ | 只认 `@本机昵称`；也可用 `!` 前缀触发（`--group-prefix`） |
| 旧消息不回 | 180s | `--max-age 0` 关闭 |
| 同会话冷却 | 5s | `--cooldown` |
| 每分钟上限 | 6 条 | `--max-per-minute` |
| 发送确认 | 默认关 | 需要时加 `--verify` / GUI 勾选；微信落库可能晚于 8 秒，开回读会慢且可能**误报“未确认”**（消息其实已发出）。无论开不开，**未确认都不会重发** |

拟人节奏由库统一控制（默认 `natural`：写动作间隔 2.5–6s、120s 内 6 次）。
想临时快一点：`$env:WECHATAUTO_RHYTHM="fast"`；默认档位不要改成 `off`。

## 4. 接入 AI（可选，已内置）

代码在 `wechatauto/ai_reply.py`，走 **OpenAI 兼容** 的 `/chat/completions`
（DeepSeek / OpenAI / Ollama / vLLM 都行）。

Key 放在仓库根目录的 **`.env`** 里（该文件已被 `.gitignore` 忽略，不会推到 GitHub）：

```ini
WECHATAUTO_AI_KEY=sk-你的key
WECHATAUTO_AI_BASE=https://api.deepseek.com/v1
WECHATAUTO_AI_MODEL=deepseek-chat
```

没有 `.env` 就复制 `.env.example`：

```powershell
Copy-Item .env.example .env
notepad .env
```

优先级：**系统环境变量 > `.env`**（`.env` 只补缺失项，不会覆盖你已有的设置）。
改完 `.env` 要**重启 GUI** 才生效。

然后：

- **GUI**：勾「未命中规则时调用 AI」，启动后日志会显示 `AI 已启用`；
- **命令行**：加 `--ai`。

匹配顺序是 **规则命中 → AI → 兜底回复**。AI 调用在「每个会话自己的监听线程」里同步进行
（带超时），只影响该会话，不会阻塞数据库轮询；超时/失败会退回兜底回复。

### 纯 AI 模式（不用模板）

- GUI：「规则文件」留空、「兜底回复」留空、勾上「用 AI 回复」；
- 命令行：`--ai`，不传 `--rules` / `--default-reply`。

这样每条消息都会走 AI。默认还会带上该会话最近 10 轮记忆（见下）。

### 会话记忆（每个会话独立）

- **私聊一份、每个群各一份**，按会话 username 隔离，互不串味；
- 群里用户消息会带发送者前缀（如 `木笛：你好`），模型能分清是谁说的；
- 默认 **10 轮 / 3000 字**，超出从最旧的丢；可在 GUI「记忆轮数」或
  `--memory-turns` 调，`0` 关闭；字符上限 `--memory-chars`；
- 持久化到 `memory/auto_reply_<账号>.json`（`memory/` 已被 gitignore），
  重启后仍记得；想清空直接删这个文件；
- 只有在 AI 成功生成回复时才写入一轮（用户消息 + 助手回复）。

**开始新对话 / 重置记忆**：发送以下任意一条，清空**当前会话**的记忆（不调模型）：

```
/new        /reset       /重置对话      重置对话
```

群里要带触发前缀：`!new`、`!重置对话`（`!` 会先被剥掉）。
清空后，之前告诉它的暗号/名字就忘了；其他会话的记忆不受影响。

> 例：私聊里说「我的暗号是苹果」，下一句问「我的暗号是什么」能答出苹果；
> 发 `/new` 后再问，就答「我这边没记录到」；同一个群的记忆是另一份。

### 节奏与长度（GUI 里都可调）

界面「节奏与限制」一栏：

| 项 | 默认 | 说明 |
|---|---|---|
| 节奏档位 | `calm` | `calm` / `natural` / `fast` |
| 写间隔 | 6–14 秒 | 两次写动作之间的最小间隔（随机区间） |
| 窗口 / 窗口内次数 | 300 秒 / 3 次 | 窗口内写满 N 次就触发冷却 |
| 触发后冷却 | 60–150 秒 | 冷却期间不发；之前那次 147 秒等待就是它 |
| 记忆轮数 / 字数 | 10 轮 / 3000 字 | 每个会话独立 |
| 单条回复上限 | 2000 字 | 超出截断并加 `…`；AI 的 `max_tokens` 也按它估算 |

命令行对应参数：

```
--rhythm calm|natural|fast
--rhythm-gap 6 14
--rhythm-burst 3
--rhythm-window 300
--rhythm-cooloff 60 150
--memory-turns 10 --memory-chars 3000
--max-reply-chars 2000
```

### 默认关闭 OCR 兜底

自动回复**默认不允许 OCR 盲点**（`WxParam.ENABLE_OCR=False`）：UIA 打不开会话时
直接报错，不会在界面上乱点（盲点可能最小化一堆窗口，甚至点到退出登录）。
确实需要 OCR 时，命令行加 `--ocr`（不推荐常开）。

Ollama 例子（写进 `.env`）：

```ini
WECHATAUTO_AI_BASE=http://127.0.0.1:11434/v1
WECHATAUTO_AI_MODEL=qwen2.5:7b
WECHATAUTO_AI_KEY=ollama
```

## 5. 群聊怎么触发

| 消息 | 是否触发 |
|---|---|
| 私聊「你好」 | ✅ 直接按规则匹配 |
| 群聊「你好」 | ❌ 没有 @ 也没有前缀 |
| 群聊「!你好」 | ✅ 去掉 `!` 后按「你好」匹配 |
| 群聊「@焦糖可可陷阱 你好」 | ✅ @ 到本机昵称 |
| 群聊「@木笛 你好」（换号登录时） | ❌ 不是当前账号的昵称 |
| 群聊「@你的群昵称 你好」 | ⚠️ 群昵称和通讯录昵称不同，需在 GUI「额外@别名」或 CLI `--mention-name` 里加上 |

- 前缀默认 `!`，可在 GUI「群触发前缀」或 CLI `--group-prefix` 改；留空则关闭前缀触发。
- 群里回复目前是**普通文本**，不是真正的 @ 提醒（真 @ 需要点选成员弹层，稳定性不足）。

## 6. 排错

| 现象 | 原因 / 处理 |
|---|---|
| 报「找不到微信主窗口」 | 微信没启动/在登录页；脚本只认 `Weixin.exe` 的主窗，不会误绑标题带「微信」的 Chrome |
| 发送失败/未确认 | 微信窗口被锁屏、最小化到托盘、或 UIA 被重置；重跑一次，或先 `wechatauto doctor` |
| 第一次查询很慢（30s~2min） | 大库首次解密，属正常；之后走增量 WAL 合并（本仓库已修） |
| 提示数据库重建 | 删 `%TEMP%\wechatauto_db\<账号>` 可强制重建；不要在解密中途杀进程 |
| 回复发错人 | 显示名重名时 `open_chat` 可能歧义；用 wxid 明确目标 |

## 7. 已知限制

- 微信必须**已登录且窗口可操作**；锁屏/远程桌面断开时发送会失败。
- 群聊回复目前是普通文本，**不是真正的 @ 提醒**（真 @ 需要点选成员弹层，稳定性不足）。
- 发送会移动/置顶微信窗口，并在发送结束后还原被最小化的遮挡窗口。
- 自动回复属于写操作，账号风控风险由使用者承担；建议小号先验证。
