#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""新模型发布雷达 — X 账号采集器
通过本机 WebBridge (127.0.0.1:10086) 抓取 7 家 AI 公司官方 X 账号最近帖子，
分类后输出 data/hotalert-data.json。仅使用标准库。
"""
import json
import random
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone

BRIDGE = "http://127.0.0.1:10086/command"
SESSION = "x-engage"
OUT_PATH = "data/hotalert-data.json"

# 公司 → (主 handle, 备选 handle)
# (公司, [候选 handle], 身份校验正则 or None)
# 身份正则匹配 profile 显示名+简介，防假冒账号；另统一要求粉丝数 >= 5000（可解析时）
HANDLES = [
    ("OpenAI / ChatGPT", ["OpenAI"], r"openai|chatgpt"),
    ("Anthropic / Claude", ["AnthropicAI"], r"anthropic|claude"),
    ("Google / Gemini", ["GeminiApp", "GoogleAI"], r"gemini|google"),
    # @kimi 已停用，@MoonshotAI 被他人占用（荷兰足球号），官方现为 @Kimi_Moonshot
    ("Moonshot / Kimi", ["kimi", "MoonshotAI", "Kimi_Moonshot"], r"kimi|moonshot|月之暗面"),
    ("阿里 / Qwen", ["Alibaba_Qwen"], r"qwen|alibaba"),
    ("MiniMax", ["MiniMax_AI", "MiniMaxAgent"], r"minimax"),
    # @ZhipuAI 为币圈假冒号（置顶多 "Buy $ZHIPU"，粉丝仅 1K），官方现为 Z.ai
    ("智谱 / GLM", ["ZhipuAI", "ChatGLM", "Zai_org"], r"glm|z\.?\s?ai|zhipu|智谱|bigmodel|chatglm"),
]
MIN_FOLLOWERS = 5000

EXTRACT_JS = r"""
(() => {
  const out = [];
  document.querySelectorAll('article[data-testid="tweet"]').forEach((a) => {
    const timeEl = a.querySelector('time');
    const t = timeEl ? timeEl.getAttribute('datetime') : '';
    let id = '';
    const linkEl = timeEl ? timeEl.closest('a') : null;
    if (linkEl) { const m = (linkEl.href||'').match(/status\/(\d+)/); if (m) id = m[1]; }
    const textEl = a.querySelector('[data-testid="tweetText"]');
    const text = textEl ? textEl.innerText : '';
    const group = a.querySelector('[role="group"]');
    const aria = group ? (group.getAttribute('aria-label') || '') : '';
    out.push({id, t, text: text.slice(0, 400), aria: aria.slice(0, 200)});
  });
  return JSON.stringify(out);
})()
"""

RELEASE_PAT = re.compile(
    r"introducing|now available|launching|released|out now|发布|推出|上线|开源|开放权重|正式发布",
    re.I,
)
UPDATE_PAT = re.compile(r"更新|升级|提升|improved|update", re.I)
VIEWS_PAT = re.compile(r"([\d,\.]+)\s*([KM]?)\s*views", re.I)


def bridge(action, args, timeout=40):
    body = json.dumps({"action": action, "args": args, "session": SESSION}).encode()
    req = urllib.request.Request(
        BRIDGE, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def classify(text):
    if RELEASE_PAT.search(text):
        return "release"
    if UPDATE_PAT.search(text):
        return "update"
    return "other"


def parse_views(aria):
    m = VIEWS_PAT.search(aria or "")
    if not m:
        return None
    num = m.group(1).replace(",", "")
    try:
        v = float(num)
    except ValueError:
        return None
    suffix = m.group(2).upper()
    if suffix == "K":
        v *= 1_000
    elif suffix == "M":
        v *= 1_000_000
    return int(v)


# 返回 {status_id: author_handle}，用于校验帖子确实属于目标账号
#（X 是 SPA，导航后旧时间线 DOM 会残留，必须按作者过滤）
AUTHOR_MAP_JS = r"""
(() => {
  const m = {};
  document.querySelectorAll('article[data-testid="tweet"]').forEach((a) => {
    const t = a.querySelector('time');
    const l = t ? t.closest('a') : null;
    if (l) { const mm = (l.href || '').match(/\/([A-Za-z0-9_]+)\/status\/(\d+)/); if (mm) m[mm[2]] = mm[1]; }
  });
  return JSON.stringify(m);
})()
"""


def author_map():
    res = bridge("evaluate", {"code": AUTHOR_MAP_JS})
    raw = (res.get("data") or {}).get("value")
    try:
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


def wait_profile_loaded(handle, timeout=20):
    """轮询直到页面上出现目标 handle 本人发的帖（SPA 旧 DOM 会残留）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            amap = author_map()
        except Exception:
            amap = {}
        if any(a.lower() == handle.lower() for a in amap.values()):
            return True
        time.sleep(2)
    return False


PROFILE_JS = r"""
(() => {
  const cell = document.querySelector('[data-testid="UserName"]');
  const name = cell ? cell.innerText.replace(/\n/g, ' | ') : '';
  const desc = document.querySelector('[data-testid="UserDescription"]');
  const fol = document.querySelector('a[href$="/verified_followers"], a[href$="/followers"]');
  return JSON.stringify({
    name: name,
    desc: desc ? desc.innerText : '',
    followers: fol ? fol.innerText : ''
  });
})()
"""


def parse_followers(s):
    m = re.search(r"([\d,\.]+)\s*([KM]?)\s*Followers", s or "", re.I)
    if not m:
        return None
    try:
        v = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    suffix = m.group(2).upper()
    if suffix == "K":
        v *= 1_000
    elif suffix == "M":
        v *= 1_000_000
    return int(v)


def profile_info():
    res = bridge("evaluate", {"code": PROFILE_JS})
    raw = (res.get("data") or {}).get("value")
    try:
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


def check_identity(identity_pat):
    """返回 (ok, reason)。身份正则 + 粉丝数下限，防假冒号。"""
    info = profile_info()
    haystack = f"{info.get('name', '')} {info.get('desc', '')}"
    if identity_pat and not re.search(identity_pat, haystack, re.I):
        return False, f"identity mismatch: {info.get('name', '')[:60]!r}"
    fol = parse_followers(info.get("followers", ""))
    if fol is not None and fol < MIN_FOLLOWERS:
        return False, f"followers too low ({fol}), possible impostor"
    return True, None


def scrape_handle(handle, identity_pat=None, max_posts=5):
    """返回 (posts, error)。posts 为 [] 表示抓取失败或无内容。"""
    bridge("navigate", {"url": f"https://x.com/{handle}", "newTab": False})
    time.sleep(7)
    if not wait_profile_loaded(handle):
        return [], "profile not loaded or no own posts visible"
    ok_id, why = check_identity(identity_pat)
    if not ok_id:
        return [], why
    for _ in range(2):
        bridge("evaluate", {"code": "window.scrollBy(0, 1200); 'ok'"})
        time.sleep(random.uniform(2.0, 3.0))
    res = bridge("evaluate", {"code": EXTRACT_JS})
    raw = (res.get("data") or {}).get("value")
    if not raw:
        return [], "no data returned"
    try:
        items = json.loads(raw)
    except Exception as e:
        return [], f"parse error: {e}"
    amap = author_map()
    posts = []
    for it in items:
        if not it.get("id"):
            continue
        author = amap.get(it["id"], "")
        if author and author.lower() != handle.lower():
            continue  # 推广帖 / 旧页面残留，非本账号所发
        text = it.get("text", "")
        posts.append(
            {
                "id": it["id"],
                "url": f"https://x.com/{handle}/status/{it['id']}",
                "time": it.get("t", ""),
                "text": text,
                "aria": it.get("aria", ""),
                "type": classify(text),
                "views": parse_views(it.get("aria", "")),
            }
        )
        if len(posts) >= max_posts:
            break
    return posts, None


def main():
    accounts = []
    events = []
    summary = []

    for idx, (company, handles, identity_pat) in enumerate(HANDLES):
        if idx > 0:
            time.sleep(random.uniform(2.0, 4.0))
        used_handle = None
        posts = []
        err = None
        for cand in handles:
            for attempt in (1, 2):  # 失败重试一次（加载超时属偶发）
                try:
                    posts, err = scrape_handle(cand, identity_pat)
                except Exception as e:
                    posts, err = [], str(e)
                if posts:
                    break
                time.sleep(random.uniform(2.0, 4.0))
            if posts:
                used_handle = cand
                break
        ok = bool(posts)
        accounts.append(
            {
                "handle": used_handle or handles[0],
                "requested_handles": handles,
                "company": company,
                "ok": ok,
                "error": None if ok else (err or "no posts found"),
                "posts": posts,
            }
        )
        n_rel = sum(1 for p in posts if p["type"] == "release")
        summary.append((company, used_handle or handles[0], ok, len(posts), n_rel))
        for p in posts:
            if p["type"] in ("release", "update"):
                events.append(
                    {
                        "time": p["time"],
                        "company": company,
                        "handle": used_handle or handles[0],
                        "type": p["type"],
                        "title": (p["text"].splitlines() or [""])[0][:120],
                        "url": p["url"],
                        "views": p["views"],
                    }
                )

    events.sort(key=lambda e: e.get("time") or "", reverse=True)
    data = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "accounts": accounts,
        "events": events,
    }
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    # file:// 下 Chrome 拦截 fetch/XHR，额外产出 JS 包装供 <script> 标签兜底加载
    with open(OUT_PATH.replace(".json", ".js"), "w", encoding="utf-8") as f:
        f.write("window.HOTALERT_DATA = ")
        json.dump(data, f, ensure_ascii=False)
        f.write(";\n")

    print("\n===== 采集汇总 =====")
    for company, handle, ok, n, n_rel in summary:
        mark = "OK " if ok else "FAIL"
        print(f"[{mark}] {company:<22} @{handle:<14} 帖子 {n} 条, release {n_rel} 个")
    print(f"events 总数: {len(events)} → {OUT_PATH}")


if __name__ == "__main__":
    sys.exit(main())
