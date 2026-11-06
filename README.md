# 微博超话自动签到

每天定时给自己的关注超话签到 + 按池子发评论/转发，并把执行结果推送到自建面板。
纯 Python 标准库，**无第三方依赖**，多账号隔离，适合长期挂在自己的机器或服务器上。

> 本项目为个人日常使用中的自动化工具，按需维护。

---

## 它解决什么

手动做这些事每天要花 5–10 分钟，而且容易漏：

- 每个关注超话逐个签到
- 挑帖子发评论 / 转发，还要避免同一帖重复
- 隔一段时间回头删掉自己发的内容（避免刷屏痕迹累积）
- 多个账号要分别执行
- 执行结果要能远程看到，不能只在本机日志里

这个仓库把上面几件事串成了一条可定时执行的流水线。

---

## 核心设计

### 1. 多账号物理隔离

每个账号一套独立目录，互不共享任何状态：

```
data/        账号 A：config.json（配置）+ state.json（当日进度）+ session.json（登录态）
data-htt/    账号 B：同上，完全独立
data-htt2/   账号 C
```

`account2_daily.py` 这类脚本通过 `--data-dir` 指定用哪一套，所以同一个进程代码可以跑任意多个账号。
`data-*/` 下的 `*.json` 在公开仓库里都以 `*.example.json` / 脱敏形式存在，真实登录态不入库。

### 2. 状态机而不是「跑一遍就忘」

`state.json` 记录**当天**每个超话已完成哪些互动帖（`comments` / `reposts` 是帖子 ID 列表），所以：

- 定时任务被触发多次，也不会重复互动同一帖
- 中途失败重跑，会从断点继续
- 跨天自动重置（key 是日期）

```
{
  "uid": "<YOUR_UID>",
  "version": 4,
  "history": { "2026-11-05": { "<topic_id>": { "comments": [...], "reposts": [...] } } }
}
```

### 3. 内容池 + 去重分配

`config.json` 里维护评论模板池、转发模板池、每个超话每日上限（`comment_limit` / `repost_limit`）。
执行时按「候选帖子池 → 随机分配不重复帖子 → 从模板池取话术」的顺序生成任务，避免同一句话刷屏。

### 4. 执行完自动回收

创建 → 记录 ID → 到期删除。`pending_deletes` / `pending_retry_deletes` 两个队列分开，删除失败会重试而不是丢掉。

### 5. 可观测性

- `ops/weibo-progress-push.mjs`：把进度推给自建面板（心跳 + 完成事件两类）
- `weibo-progress-heartbeat.xml` / `weibo-progress-push.xml`：Windows 计划任务定义，和主任务分开调度，互不阻塞

---

## 文件说明

| 文件 | 作用 |
|---|---|
| `account2_daily.py` | 单账号日任务主入口（签到 + 互动 + 回收） |
| `data-*/config.json` | 该账号的配置：执行时间、上限、模板池、目标超话 |
| `data-*/state.json` | 该账号当日进度（断点续跑依据） |
| `weibo-exp*.xml` | Windows 任务计划程序的任务定义 |
| `ops/weibo-progress-push.mjs` | 进度上报脚本（Node，推送到自建面板） |
| `使用说明.md` | 逐步部署说明 |

---

## 快速开始

```bash
# 1. 复制一份配置样例
cp data-htt/config.json data/config.json
cp data-htt/state.json  data/state.json

# 2. 填自己的登录态（session.json 不入库，自行放置）
# 3. 跑一次
python account2_daily.py --data-dir data --dry-run   # 先看要做什么
python account2_daily.py --data-dir data             # 真跑
```

定时：把 `weibo-exp.xml` 里的路径改成自己的项目绝对路径，导入任务计划程序。

---

## 已知边界

- 只做了 Windows 计划任务的模板；Linux 下换成 cron 即可，脚本本身与平台无关
- 登录态会过期，脚本检测到失效时只记录、不自动重登（避免风控）
- 互动频率靠配置里的额度控制，不内置自适应限速

---

## License

MIT
