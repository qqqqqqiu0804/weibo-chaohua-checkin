"""
第二账号（UID 8353991856）每日任务：黄婷婷超话签到 + 评论 + 带超话 tag 的转发。

与 weibo-exp 的区别：
  * 评论/转发发布后【不删除】（原版 interaction.go 硬编码删除）
  * 转发 content 带超话题标签，默认 #黄婷婷[超话]#
  * 复用 weibo-exp 的 session.json / config.json / state.json 格式，
    因此 `weibo-exp --data-dir data-htt2 status` 仍能正确显示今日进度。

用法：
  python -X utf8 account2_daily.py --dry-run    只读预演，不发写请求
  python -X utf8 account2_daily.py              正式执行（幂等，今日完成则跳过）
  python -X utf8 account2_daily.py --force      忽略今日进度，补跑一轮
  python -X utf8 account2_daily.py --tag '#黄婷婷超话#'   自定义 tag
"""

import argparse
import datetime
import hashlib
import json
import os
import pathlib
import random
import re
import sys
import time
import urllib.parse

import requests

ROOT = pathlib.Path(__file__).resolve().parent
DATA = ROOT / "data-htt2"
SESSION_PATH = DATA / "session.json"
CONFIG_PATH = DATA / "config.json"
STATE_PATH = DATA / "state.json"
LOG_PATH = DATA / "logs" / "account2.log"
BASE = "https://m.weibo.cn"
TOPIC_ID = "100808b0cbb89cb1591ec01a568798e0d788ed"
TOPIC_NAME = "黄婷婷超话"
DEFAULT_TAG = "#黄婷婷[超话]#"
UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")

lines = []


def log(msg):
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = "[%s] %s" % (stamp, msg)
    print(line, flush=True)
    lines.append(line)


def load_json(path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# ---------- cookie ----------
def cookie_key(c):
    return (c.get("domain", "").lstrip(".").lower(), c.get("path") or "/", c.get("name", ""))


def cookies_for(url, cookies):
    """按 Chromium cookie 规则挑出适用于该 URL 的 cookie，同名取最具体 domain。"""
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path or "/"
    picked = {}
    for c in cookies:
        d = (c.get("domain") or "").lstrip(".").lower()
        p = c.get("path") or "/"
        if c.get("host_only"):
            ok = (d == host)
        else:
            ok = (host == d or host.endswith("." + d))
        if not ok or not path.startswith(p):
            continue
        if c.get("secure") and parsed.scheme != "https":
            continue
        key = c.get("name", "")
        prev = picked.get(key)
        if prev is None or len(d) > len(prev[0]):
            picked[key] = (d, c.get("value", ""))
    return "; ".join("%s=%s" % (k, v[1]) for k, v in picked.items() if v[1])


def merge_set_cookie(cookies, resp_cookies):
    """把服务器 Set-Cookie 合并回 session.json（cookie 续期，格式与 weibo-exp 兼容）。"""
    changed = False
    index = {cookie_key(c): c for c in cookies}
    for rc in resp_cookies:
        domain = (rc.domain or "").lstrip(".").lower()
        if not domain:
            domain = urllib.parse.urlparse(BASE).hostname
        host_only = not rc.domain
        entry = {
            "host_only": host_only,
            "name": rc.name,
            "value": rc.value,
            "domain": domain,
            "path": rc.path or "/",
            "secure": bool(rc.secure),
            "http_only": bool(getattr(rc, "_rest", {}).get("HttpOnly")),
        }
        if rc.expires:
            entry["expires"] = rc.expires
        k = cookie_key(entry)
        if index.get(k) != entry:
            index[k] = entry
            changed = True
    if changed:
        return list(index.values()), True
    return cookies, False


# ---------- 微博接口 ----------
class Weibo:
    def __init__(self, cookies, dry_run=False):
        self.cookies = cookies
        self.dry_run = dry_run
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Referer": BASE + "/",
            "X-Requested-With": "XMLHttpRequest",
        })

    def request(self, method, target, form=None):
        url = target if target.startswith("http") else BASE + ("" if target.startswith("/") else "/") + target
        headers = {"Cookie": cookies_for(url, self.cookies)}
        if method == "POST":
            headers["Content-Type"] = "application/x-www-form-urlencoded;charset=UTF-8"
        r = self.s.request(method, url, data=form, headers=headers, timeout=30)
        self.cookies, _ = merge_set_cookie(self.cookies, r.cookies)
        if r.status_code < 200 or r.status_code >= 300:
            raise RuntimeError("%s %s -> HTTP %s" % (method, urllib.parse.urlparse(url).path, r.status_code))
        text = r.text.lstrip()
        if text.startswith("<"):
            raise RuntimeError("%s 返回 HTML，可能登录失效或触发风控" % urllib.parse.urlparse(url).path)
        try:
            payload = r.json()
        except Exception:
            raise RuntimeError("%s 响应不是 JSON: %s" % (urllib.parse.urlparse(url).path, text[:120]))
        if payload.get("ok") == -100:
            raise RuntimeError("登录态失效（ok=-100），需要重新 login")
        if payload.get("ok") == 0:
            raise RuntimeError("%s 接口失败: %s" % (urllib.parse.urlparse(url).path, payload.get("msg", "")))
        return payload.get("data", payload)

    def get(self, target):
        return self.request("GET", target)

    def post(self, target, form):
        if self.dry_run:
            return {}
        return self.request("POST", target, form)


def sval(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else ""
    if isinstance(v, (int, float)):
        return "" if v == 0 else str(int(v))
    return str(v)


def parse_topics(data):
    out = []
    seen = set()

    def visit(card):
        for nested in card.get("card_group") or []:
            if isinstance(nested, dict):
                visit(nested)
        title = sval(card.get("title_sub"))
        if not title:
            return
        itemid = sval(card.get("itemid"))
        scheme = sval(card.get("scheme"))
        m = re.search(r"100808[0-9a-z]{32}", scheme + " " + itemid, re.I)
        if not m:
            return
        tid = m.group(0)
        if tid in seen:
            return
        btn_name, btn_url = "", ""
        for b in card.get("buttons") or []:
            n, u = sval(b.get("name")), sval(b.get("scheme"))
            if not btn_name:
                btn_name, btn_url = n, u
            if "签到" in n or "已签" in n:
                btn_name, btn_url = n, u
                break
        if not (itemid.startswith("follow_super_follow_") or "签到" in btn_name or "已签" in btn_name):
            return
        seen.add(tid)
        out.append({
            "id": tid, "name": title, "button": btn_name, "url": btn_url,
            "signed": "已签" in btn_name,
            "can_checkin": "签到" in btn_name and "已签" not in btn_name,
        })

    for c in data.get("cards") or []:
        if isinstance(c, dict):
            visit(c)
    return out


def parse_posts(data, self_uid):
    out = []
    seen = set()

    def visit(card):
        for nested in card.get("card_group") or []:
            if isinstance(nested, dict):
                visit(nested)
        mb = card.get("mblog")
        if not isinstance(mb, dict):
            return
        mid = sval(mb.get("mid")) or sval(mb.get("id"))
        if not mid or mid in seen:
            return
        seen.add(mid)
        uid = sval((mb.get("user") or {}).get("id"))
        out.append({"mid": mid, "is_self": bool(self_uid) and uid == self_uid})

    for c in data.get("cards") or []:
        if isinstance(c, dict):
            visit(c)
    return out


def created_id(data):
    if not isinstance(data, dict):
        return ""
    dm = data.get("data") if isinstance(data.get("data"), dict) else {}
    cands = [dm.get("idstr"), dm.get("id"), dm.get("mid")]
    for key in ("comment", "status"):
        sub = data.get(key) or dm.get(key)
        if isinstance(sub, dict):
            cands += [sub.get("idstr"), sub.get("id"), sub.get("mid")]
    cands += [data.get("idstr"), data.get("id"), data.get("mid")]
    for c in cands:
        v = sval(c)
        if v:
            return v
    return ""


def choose_template(post_mid, date_topic, templates):
    if not templates:
        return "打卡"
    h = 2166136261
    for ch in (date_topic + ":" + post_mid):
        h ^= ord(ch)
        h = (h * 16777619) & 0xFFFFFFFF
    return templates[h % len(templates)]


# ---------- 主流程 ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--tag", default=DEFAULT_TAG)
    args = ap.parse_args()

    cfg = load_json(CONFIG_PATH, {})
    comment_limit = int(cfg.get("comment_limit", 4))
    repost_limit = int(cfg.get("repost_limit", 2))
    templates = cfg.get("comment_templates") or ["打卡"]
    max_feed_pages = int(cfg.get("max_feed_pages", 8))
    max_posts = max(int(cfg.get("max_posts_per_topic", 80)), comment_limit + repost_limit)

    session = load_json(SESSION_PATH, {"version": 2, "cookies": []})
    cookies = session.get("cookies") or []
    if not cookies:
        log("FATAL: session.json 无 cookie，请重新 weibo-exp login")
        return 2

    today = datetime.date.today().strftime("%Y-%m-%d")
    state = load_json(STATE_PATH, {"version": 4, "history": {}, "uid": ""})
    state.setdefault("history", {})
    # 只保留最近 7 天，避免无限累积（与 weibo-exp 的 HistoryRetentionDays 一致）
    cutoff = (datetime.date.today() - datetime.timedelta(days=6)).strftime("%Y-%m-%d")
    state["history"] = {k: v for k, v in state["history"].items() if k >= cutoff}
    if args.force:
        state["history"].pop(today, None)
    day = state["history"].setdefault(today, {})
    rec = day.setdefault(TOPIC_ID, {"comments": [], "reposts": []})
    rec.setdefault("comments", [])
    rec.setdefault("reposts", [])

    wb = Weibo(cookies, dry_run=args.dry_run)

    log("=== 第二账号每日任务 uid=%s 日期=%s dry_run=%s tag=%s ===" %
        (state.get("uid", "?"), today, args.dry_run, args.tag))

    # 1) 登录态
    conf = wb.get("/api/config")
    if not conf.get("login"):
        raise RuntimeError("微博返回 login=false，需要重新扫码")
    st = sval(conf.get("st"))
    uid = sval(conf.get("uid"))
    if not st:
        raise RuntimeError("/api/config 未返回 st")
    if state.get("uid") and state["uid"] != uid:
        raise RuntimeError("state.json 绑定 uid=%s，当前登录 uid=%s，拒绝跨账号写入" % (state["uid"], uid))
    state["uid"] = uid
    log("登录态：login=true uid=%s st=%s..." % (uid, st[:8]))

    # 2) 幂等
    need_comments = max(0, comment_limit - len(rec["comments"]))
    need_reposts = max(0, repost_limit - len(rec["reposts"]))
    if not args.force and need_comments == 0 and need_reposts == 0:
        log("今日已完成（评论 %d/%d，转发 %d/%d）；本次不执行。" %
            (len(rec["comments"]), comment_limit, len(rec["reposts"]), repost_limit))
        return 0

    # 3) 关注超话 + 签到
    followed = wb.get("/api/container/getIndex?containerid=100803_-_followsuper")
    topics = parse_topics(followed)
    topic = next((t for t in topics if t["id"] == TOPIC_ID), None)
    if topic is None:
        log("FATAL: 关注列表里没有 %s（ID %s），请先在该账号关注超话" % (TOPIC_NAME, TOPIC_ID))
        return 3
    log("超话：%s；按钮=%s；今日已签到=%s" % (topic["name"], topic["button"], topic["signed"]))
    if topic["can_checkin"] and topic["url"]:
        if args.dry_run:
            log("[预演] 将执行签到：%s" % topic["url"][:120])
        else:
            wb.get(topic["url"])
            log("签到：成功")
    elif topic["signed"]:
        log("签到：今日已签到，跳过")
    else:
        log("签到：无可用入口（按钮=%s），继续互动" % topic["button"])

    # 4) 候选帖子
    posts, cursor, seen_cursor, pages = [], "", set(), 0
    while len(posts) < max_posts and pages < max_feed_pages:
        path = "/api/container/getIndex?containerid=" + urllib.parse.quote(TOPIC_ID)
        if cursor:
            path += "&since_id=" + urllib.parse.quote(cursor)
        data = wb.get(path)
        batch = parse_posts(data, uid)
        posts.extend(p for p in batch if not p["is_self"])
        nxt = sval((data.get("cardlistInfo") or {}).get("since_id"))
        pages += 1
        if not nxt or nxt == cursor or nxt in seen_cursor:
            break
        seen_cursor.add(nxt)
        cursor = nxt

    done = set(rec["comments"]) | set(rec["reposts"])
    usable = []
    seen_mid = set()
    for p in posts:
        if p["mid"] and p["mid"] not in done and p["mid"] not in seen_mid:
            seen_mid.add(p["mid"])
            usable.append(p["mid"])
    log("候选帖子：%d 条（已排除本人/今日已互动）；待发 评论 %d、转发 %d" %
        (len(posts), need_comments, need_reposts))

    comment_targets = usable[:need_comments]
    repost_targets = usable[len(comment_targets):len(comment_targets) + need_reposts]
    if len(comment_targets) < need_comments or len(repost_targets) < need_reposts:
        log("警告：可用帖子不足，只能分配 评论 %d/%d、转发 %d/%d" %
            (len(comment_targets), need_comments, len(repost_targets), need_reposts))

    repost_content = (args.tag + " 转发微博").strip()
    log("转发内容模板：%r（tag=%s，发布后不删除）" % (repost_content, args.tag))

    if args.dry_run:
        log("[预演] 评论 %d 条、转发 %d 条（含 tag），不执行任何写操作，也不会调用删除接口" %
            (len(comment_targets), len(repost_targets)))
        return 0

    # 5) 评论（保留，不删除）
    for i, mid in enumerate(comment_targets, 1):
        content = choose_template(mid, today + ":" + TOPIC_ID, templates)
        data = wb.post("/api/comments/create", {"content": content, "mid": mid, "st": st})
        cid = created_id(data)
        rec["comments"].append(mid)
        save_json(STATE_PATH, state)
        log("评论 [%d/%d]：已发布并保留（原帖 %s，内容 %s，新内容 %s）" %
            (i, len(comment_targets), mid, content, cid or "?"))
        time.sleep(random.uniform(8, 14))

    # 6) 转发（带 tag，保留，不删除）
    for i, mid in enumerate(repost_targets, 1):
        data = wb.post("/api/statuses/repost", {
            "id": mid, "mid": mid, "content": repost_content, "dualPost": "0", "st": st,
        })
        rid = created_id(data)
        rec["reposts"].append(mid)
        save_json(STATE_PATH, state)
        log("转发 [%d/%d]：已发布并保留（原帖 %s，内容 %r，新内容 %s）" %
            (i, len(repost_targets), mid, repost_content, rid or "?"))
        # 回读确认 tag 确实落在转发文本里
        if rid:
            try:
                detail = wb.get("/api/statuses/show?id=" + urllib.parse.quote(rid))
                text = ""
                if isinstance(detail, dict):
                    text = sval(detail.get("text")) or sval((detail.get("data") or {}).get("text"))
                log("  回读校验：转发文本=%r；包含 tag=%s" % (text[:160], args.tag in text))
            except Exception as e:
                log("  回读校验跳过（%s）" % str(e)[:120])
        time.sleep(random.uniform(8, 14))

    # 7) 回写会话（cookie 续期），保持 weibo-exp 兼容
    session["cookies"] = wb.cookies
    # Go 的 time.Time 要求 RFC3339（带时区），否则 weibo-exp 读 session.json 会报错
    session["updated_at"] = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    save_json(SESSION_PATH, session)

    state["version"] = 4
    state.pop("pending_deletes", None)
    save_json(STATE_PATH, state)
    log("汇总：签到=%s 评论=%d（保留） 转发=%d（保留，带 tag） 删除=0" %
        ("已处理", len(rec["comments"]), len(rec["reposts"])))
    return 0


if __name__ == "__main__":
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    code = 1
    try:
        code = main()
    except Exception as e:
        log("FATAL %s: %s" % (type(e).__name__, e))
    finally:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    sys.exit(code)
