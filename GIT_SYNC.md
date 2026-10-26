# Git 同步说明（fork 工作流）

这个仓库现在的 remote：

| remote | 指向 | 用途 |
|---|---|---|
| `origin` | `git@github.com:ANgdi/wechatauto-replica.git` | 你的 fork，日常推这里 |
| `upstream` | `git@github.com:fanyuantaier/wechatauto-replica.git` | 原作者，只读，用来拉更新 |

**不要用「下载 zip 逐个文件对比」**。Git 会做三方合并：只有同一文件的同一段被两边改过才需要手动解决，其余自动合并。

## 日常

```powershell
cd D:\Gitspace\wechatauto-replica
git status                 # 看有没有未提交改动
git add -A
git commit -m "说明"
git push origin main
```

## 同步原作者的新提交（推荐：merge）

```powershell
cd D:\Gitspace\wechatauto-replica
git fetch upstream                 # 只下载，不动你的工作区
git log --oneline main..upstream/main   # 先看上游多了哪些提交（可选）
git merge upstream/main            # 合并进当前 main
git push origin main               # 推到你的 fork
```

有冲突时：

```powershell
git status                         # 列出冲突文件
# 手动编辑冲突文件，保留两边需要的逻辑
git add <冲突文件>
git commit                         # merge 会自动生成提交信息
git push origin main
```

想放弃这次合并：`git merge --abort`。

## 同步上游（进阶：rebase，历史更干净）

如果希望你的提交始终排在 upstream 之后、没有 merge 提交：

```powershell
git fetch upstream
git rebase upstream/main
# 有冲突：改完 -> git add <文件> -> git rebase --continue
# 想放弃：git rebase --abort
git push --force-with-lease origin main
```

> `--force-with-lease` 比 `--force` 安全：远端在你 fetch 之后被别人改过时它会拒绝。

## 只看上游改了什么

```powershell
git fetch upstream
git diff main...upstream/main --stat      # 上游相对你的改动概览
git log --oneline --graph --all -20       # 分支关系
```

## 冲突时的注意点

- `wechatauto/db.py` 是本 fork 改动最大的文件（增量 WAL 合并 + 锁序）。上游若也改这里，冲突要**逐段看语义**，不要整文件选一边：
  - 保留上游的修复逻辑；
  - 同时保留 `STAMP_VERSION` 递增、`_try_incremental_wal`、`_CACHE_IO_LOCK` 这套增量/锁逻辑。
- 新增文件（`demo_auto_reply.py`、`auto_reply_gui.py`、文档、规则示例）上游一般不会碰，基本不会冲突。
- 冲突解决后跑一遍：`python -m unittest discover -s tests` 和 `python -m wechatauto.demo_auto_reply --self-test`。

## 一句话版本

以后原作者更新，你只需要：

```powershell
git fetch upstream
git merge upstream/main
git push origin main
```

不是下载对比，是三方合并。冲突只可能出现在两边都改过的同一段代码。
