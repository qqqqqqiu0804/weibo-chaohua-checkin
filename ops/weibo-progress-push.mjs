#!/usr/bin/env node
/**
 * ops/weibo-progress-push.mjs —— 把本机微博超话签到任务的进度推到 NexusCore。
 * ====================================================================
 * 数据源：E:/vibecoding/harness/weibo-exp/{data,data-htt,data-htt2}
 *   · config.json  → 计划时间、评论/转发条数、超话范围
 *   · state.json   → history[日期][超话id] = { comments:[], reposts:[] }
 *   · logs/        → 「今日已完成 N/M 个超话」权威口径（state 里 data/ 全量签到不落 history）
 *
 * 目标：POST /api/weibo/progress，服务端合并进 KV 单键 weiboProgress。
 *
 * 用法：
 *   node ops/weibo-progress-push.mjs
 *   node ops/weibo-progress-push.mjs --dry-run
 *
 * ★ 为什么「已完成数」优先取日志而不是 state.history：
 *   data/ 那套是「全关注超话签到」（8 个），它从不往 state.history 里写
 *   （签到不是评论/转发，没有要去的重复）。只看 history 会把它算成 0/1。
 *   日志里那句「今日已完成 8/8 个超话」才是这套任务自己的口径。
 *
 * ★ 单向只读（对浏览器而言）：本脚本写、页面只渲染。
 */

import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';

const CFG = {
  apiBase: process.env.NEXUS_API_BASE || 'https://nexus.kotete.xyz',
  token: process.env.NEXUS_TOKEN || '',
  projectDir: process.env.WEIBO_DIR || 'E:/vibecoding/harness/weibo-exp',
  host: process.env.NEXUS_HOST || os.hostname().slice(0, 64),
};

const args = new Set(process.argv.slice(2));
const DRY = args.has('--dry-run');

/** 三套任务目录 → 展示名。标签可在环境变量里改。 */
const ACCOUNTS = [
  { id: 'data', label: process.env.WEIBO_LABEL_DATA || '账号A · 全超话签到', keep: false },
  { id: 'data-htt', label: process.env.WEIBO_LABEL_HTTP || '账号A · 黄婷婷互动', keep: false },
  { id: 'data-htt2', label: process.env.WEIBO_LABEL_HTTP2 || '账号B · 黄婷婷签到+互动', keep: true },
];

function readJson(file, fallback = null) {
  try { return JSON.parse(fs.readFileSync(file, 'utf8')); } catch { return fallback; }
}

function todayShanghai() {
  const now = new Date(Date.now() + 8 * 3600 * 1000);
  return now.toISOString().slice(0, 10);
}

/** 日志尾部：最后一行 + 「今日已完成 N/M 个超话」里的两个数字 */
function readLogs(dir) {
  let last = null;
  let done = null;
  let total = null;
  let lastAt = null;
  for (const name of ['account2.log', 'scheduled.log']) {
    const f = path.join(dir, 'logs', name);
    if (!fs.existsSync(f)) continue;
    try {
      const lines = fs.readFileSync(f, 'utf8').trim().split(/\r?\n/).filter(Boolean);
      if (!lines.length) continue;
      last = lines[lines.length - 1].slice(0, 160);
      const stamp = last.match(/\[([\d-]+ [\d:]+)\]/);
      if (stamp) {
        const t = Date.parse(stamp[1].replace(' ', 'T') + '+08:00');
        if (Number.isFinite(t)) lastAt = t;
      }
      // 「今日已完成 8/8 个超话」 / 「今日已完成（评论 4/4，转发 2/2）」
      const m = [...lines].reverse()
        .map(l => l.match(/今日已完成\s*(\d+)\/(\d+)\s*个超话/))
        .find(Boolean);
      if (m) { done = Number(m[1]); total = Number(m[2]); }
    } catch { /* ignore */ }
  }
  return { last, done, total, lastAt };
}

function loadAccount(spec) {
  const dir = path.join(CFG.projectDir, spec.id);
  const cfg = readJson(path.join(dir, 'config.json'));
  if (!cfg) return null;

  const state = readJson(path.join(dir, 'state.json'), {});
  const today = todayShanghai();
  const todayState = (state && state.history && state.history[today]) || {};

  const selected = Array.isArray(cfg.selected_topics)
    ? cfg.selected_topics.map(t => String(t.name || '')).filter(Boolean)
    : [];

  // history 里记了「已处理过」的超话（评论/转发用）
  const topicIds = Object.keys(todayState || {});
  let comments = 0;
  let reposts = 0;
  for (const t of topicIds) {
    const rec = todayState[t] || {};
    comments += Array.isArray(rec.comments) ? rec.comments.length : 0;
    reposts += Array.isArray(rec.reposts) ? rec.reposts.length : 0;
  }

  const logs = readLogs(dir);

  // 权威口径优先级：日志的 N/M > history 的 topicIds/selected
  let completed;
  let inScope;
  if (logs.done !== null && logs.total !== null) {
    completed = logs.done;
    inScope = logs.total;
  } else if (selected.length) {
    completed = topicIds.length;
    inScope = selected.length;
  } else {
    completed = topicIds.length;
    inScope = topicIds.length || 1;
  }

  return {
    id: spec.id,
    uid: String((state && state.uid) || ''),
    label: spec.label + (spec.keep ? '（保留）' : ''),
    time: String(cfg.scheduled_time || '').slice(0, 5),
    comment: Number(cfg.comment_limit) || 0,
    repost: Number(cfg.repost_limit) || 0,
    scope: selected.length ? 'selected' : 'all',
    selected,
    today,
    complete: inScope > 0 && completed >= inScope,
    completed,
    inScope,
    pending: 0,
    signed: completed,
    lastRunAt: logs.lastAt || Date.now(),
    lastResult: logs.last,
    nextRunAt: null,
  };
}

const accounts = ACCOUNTS.map(loadAccount).filter(Boolean);
const payload = { host: CFG.host, today: todayShanghai(), heartbeat: true, accounts };

if (DRY) {
  console.log('[dry-run] payload:');
  console.log(JSON.stringify(payload, null, 2));
  process.exit(0);
}

if (!CFG.token) {
  console.error('缺少 NEXUS_TOKEN。');
  process.exit(2);
}

try {
  const resp = await fetch(`${CFG.apiBase.replace(/\/+$/, '')}/api/weibo/progress`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${CFG.token}` },
    body: JSON.stringify(payload),
  });
  const text = await resp.text();
  if (!resp.ok) throw new Error(`HTTP ${resp.status}: ${text.slice(0, 300)}`);
  const done = accounts.filter(a => a.complete).length;
  console.log(`推送成功：${done}/${accounts.length} 套任务已完成 · ${text.slice(0, 160)}`);
} catch (err) {
  console.error('推送失败：', err.message);
  process.exit(1);
}
