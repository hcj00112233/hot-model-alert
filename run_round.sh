#!/bin/bash
# 新模型发布雷达：launchd 每 30 分钟（14:00-次日03:00）触发
# 采集 → 推送 GitHub（更新网页）→ 90 分钟内的 release 弹 macOS 通知
cd /Users/moonshot/hot-model-alert || exit 1

/usr/bin/python3 collect.py > /tmp/hotalert-collect.log 2>&1

git add -A
git commit -qm "auto: data refresh" 2>/dev/null
git push -q 2>/dev/null

/usr/bin/python3 - <<'EOF'
import json, datetime, os, subprocess
base = os.path.expanduser("~/hot-model-alert")
try:
    d = json.load(open(os.path.join(base, "data/hotalert-data.json")))
except Exception:
    raise SystemExit(0)
state = os.path.join(base, "data/alerted_ids.txt")
seen = set()
if os.path.exists(state):
    seen = set(open(state).read().split())
now = datetime.datetime.now(datetime.timezone.utc)
fresh = []
for e in d.get("events", []):
    try:
        dt = datetime.datetime.fromisoformat(e.get("time", "").replace("Z", "+00:00"))
    except Exception:
        continue
    age = (now - dt).total_seconds() / 60
    sid = (e.get("url") or "").rsplit("/", 1)[-1]
    if age < 90 and sid and sid not in seen:
        fresh.append((sid, e))
for sid, e in fresh:
    title = ("新模型发布: " + str(e.get("company", ""))).replace('"', "'")
    msg = ((e.get("title") or "")[:90] + " " + (e.get("url") or "")).replace('"', "'")
    subprocess.run(["osascript", "-e", 'display notification "%s" with title "%s"' % (msg, title)])
if fresh:
    with open(state, "a") as f:
        for sid, _ in fresh:
            f.write(sid + "\n")
EOF
