#!/usr/bin/env python3
"""
生成「中文用户向」更新日志，供 manifest / GitHub Release / FnDepot 索引共用。

为什么不能直接搬上游 CHANGELOG（实测数据）：
  上游 CHANGELOG.md 只有 5 个日期小节，而 main 有 34 个日期有提交，
  151 条提交里 133 条（88%）没有对应条目；并且它是 Markdown 英文技术日志，
  而 manifest 的 changelog 是**单行 INI 值**，官方要求「简洁、面向用户」。

因此策略是双源 + 优先级：
  1. 上游 CHANGELOG 里落在 [上次 pin, 本次 pin] 日期区间内的小节 → 取首条，人工摘要过
     的信息量最高，但区间常常对不上（实测 1.1.0→1.1.1 只有 1 个提交，日期落在两小节之间）；
  2. 区间内的 git commit（conventional commits）→ 映射成中文用户向条目；
  3. 都没有 → 兜底「跟随上游更新（<短 SHA>）」。

用法:
    python3 gen-changelog.py --repo halfwaystudent/douyin-sparkflow \
        --prev-sha <40hex> --new-sha <40hex> --manifest manifest --out changelog.txt

    # 本地/CI 都能跑；--offline-cache 可复用上次抓到的 JSON，避免重复打 API
环境变量:
    GITHUB_TOKEN   可选，提高 API 限额
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# ── conventional commit 前缀 → 中文标签 ──────────────────────────────
# 只收用户能感知的类型；docs/test/ci/chore/refactor 对最终用户无意义。
TYPE_LABEL = {
    "fix": "修复",
    "feat": "新增",
    "perf": "优化",
}
# 明确丢弃的类型（不是丢整条提交，是只丢它的文案理由）
SKIP_TYPES = {"docs", "test", "ci", "chore", "refactor", "style", "build", "merge"}

# 每个前缀最多取几条，防止一次上游大版本把 changelog 撑爆
MAX_PER_TYPE = 4
MAX_TOTAL = 8

# manifest 的 changelog 是单行 INI 值，长度上限按应用中心能正常展示来定
MAX_LEN = 420


def api(url: str, attempts: int = 6) -> object:
    """
    GitHub API GET。

    必须处理 IncompleteRead：上游 compare 30 个提交时响应超过 600 KB，
    分块传输会在流末被掐断（实测 "626114 bytes read, 16674 more expected"），
    这是传输层失败而非内容差异，退避后重试即可 —— 直接放弃会让整个构建中断。
    """
    last = None
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "fnos-fpk-changelog",
                    **({"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}"}
                       if os.environ.get("GITHUB_TOKEN") else {}),
                },
            )
            with urllib.request.urlopen(req, timeout=90) as resp:
                # 读到 EOF 为止；不依赖单次 read() 返回完整 body
                chunks = []
                while True:
                    chunk = resp.read(1 << 16)
                    if not chunk:
                        break
                    chunks.append(chunk)
            return json.loads(b"".join(chunks).decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - 统一重试
            last = exc
            wait = min(2 ** attempt, 30)
            print(f"  [重试 {attempt + 1}/{attempts}] {type(exc).__name__}，{wait}s 后再试",
                  file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"GitHub API 重试 {attempts} 次仍失败: {url}: {last}")


def split_conventional(message: str):
    """返回 (type, subject)；非 conventional 返回 (None, 首行)。"""
    first = message.strip().splitlines()[0] if message.strip() else ""
    m = re.match(r"^(\w+)(\([^)]*\))?!?:\s*(.+)$", first)
    if not m:
        # 无前缀但首字母大写（Fix xxx / Added xxx …）也算用户向
        return None, first
    return m.group(1).lower(), m.group(3).strip()


def clean_subject(subject: str) -> str:
    """去掉尾部 PR 号、句号、多余空白。"""
    s = re.sub(r"\s*\(#\d+\)\s*$", "", subject)
    s = re.sub(r"\s*\[(?:skip ci)\]\s*$", "", s, flags=re.I)
    return s.strip().rstrip(".").strip()


# ── 领域词表：英文 → 中文用户向措辞 ──────────────────────────────────
# 机器翻译会把 "treat a refused browser send as a failure" 译成
# 「将浏览器发送视为失败」这类不可读的中文，因此维护一张**只覆盖本项目高频关键词**
# 的词表；命中不了的整条退回英文原文 —— 宁可输出英文，也不输出错译的假中文。
VOCAB = {
    # 账号 / 登录
    "logins": "登录", "login": "登录", "relogin": "重新登录", "relogins": "重新登录",
    "account": "账号", "accounts": "账号", "sub-account": "子账号",
    "identity": "身份", "credential": "凭据", "session": "会话",
    "qr": "二维码", "qr code": "二维码",
    # 发送火花
    "send": "发送", "sends": "发送", "sent": "发送", "sending": "发送",
    "spark": "火花", "sparkflow": "火花", "streak": "火花",
    "browser": "浏览器", "protocol": "协议", "web": "网页", "console": "控制台",
    "page": "页面", "button": "按钮", "dialog": "弹窗", "notice": "提示",
    "setting": "设置", "settings": "设置", "panel": "面板", "dashboard": "面板",
    "friend": "好友", "friends": "好友", "friend list": "好友列表",
    "scan": "扫描", "scanned": "扫描", "verifier": "验证码", "verify": "验证",
    # 结果 / 状态
    "failure": "失败", "failed": "失败", "fail": "失败", "error": "错误",
    "success": "成功", "refused": "被拒", "rejected": "被拒",
    "timeout": "超时", "time out": "超时", "times out": "超时",
    "expired": "过期", "expiry": "过期", "stale": "失效", "dead": "失效",
    "retry": "重试", "resend": "重新发送", "resend-all": "全部重发",
    "cooldown": "冷却", "cooldowns": "冷却", "pause": "暂停", "paused": "已暂停",
    "cancel": "取消", "refresh": "刷新", "reload": "重新加载", "navigate": "跳转",
    "progress": "进度", "result": "结果", "status": "状态", "state": "状态",
    "scheduling": "定时调度", "schedule": "定时调度", "scheduler": "定时调度",
    "notification": "通知", "notifications": "通知", "webhook": "机器人推送",
    "telegram": "Telegram", "dingtalk": "钉钉", "wecom": "企业微信",
    "marker": "标记", "lock": "锁", "pid": "进程",
    "profile": "浏览器配置", "proxy": "代理", "cache": "缓存",
    "performance": "性能", "faster": "更快", "speed": "速度",
    "mobile": "手机", "phone": "手机", "desktop": "桌面", "popup": "弹窗",
    "detail": "明细", "summary": "汇总", "daily summary": "每日汇总",
    "target": "目标", "targets": "目标", "unconfirmed": "未确认", "confirmed": "已确认",
    "network": "网络", "localhost": "本机", "direct": "直连", "proxies": "代理",
    "keyboard": "键盘", "focus ring": "焦点框", "tab bar": "底部标签栏",
    "start": "启动", "startup": "启动", "idle": "空闲", "shutdown": "关闭",
    "responsive": "响应式", "config": "配置", "environment": "环境",
    "secret": "密钥", "secrets": "密钥", "download": "下载", "upload": "上传",
}

# 结构虚词：丢弃，不参与翻译
_STOPWORDS = {
    "a", "an", "the", "to", "of", "in", "on", "at", "by", "for", "with", "and", "or",
    "not", "as", "into", "from", "up", "out", "off", "over", "under", "its", "it",
    "every", "once", "per", "dash", "short", "long", "own", "self", "is", "are",
    "was", "were", "be", "been", "being", "only", "also", "just", "dead", "live",
}

# 人工确认的中文整句（精确匹配，键为小写英文 subject）
_PHRASE = {
    "treat a refused browser send as a failure, not a success":
        "抖音发送被拒绝时不再误记为成功",
    "treat a qr request that times out as a busy page, not a dead service":
        "取二维码超时时显示「处理中」而非误判服务异常",
    "bind relogins to verified douyin identity":
        "重新登录时校验抖音身份，避免登错账号",
    "read real friend names and only trust a complete scan":
        "好友列表读取真实昵称，且仅在扫描完整时采信结果",
    "do not read a failed page load as a logout":
        "页面加载失败不再被误判为掉线",
    "let a sub-account manage its own douyin accounts":
        "支持子账号管理自己的抖音账号",
    "save a scanned login at once and verify it in the background":
        "扫码登录后立即保存，并在后台校验登录态",
    "open the login workspace faster and show progress from the click":
        "扫码登录页打开更快，点击后立即显示进度",
    "push run failures and daily summaries to im bots":
        "新增运行失败提醒与每日汇总推送到 IM 机器人",
    "keep the qr wait alive through page navigation and never start on a stale login":
        "等待二维码期间页面跳转不再中断，也不会从未登录态开始扫描",
    "answer a login save at once and show it is working":
        "保存登录态立即有反馈并显示进行中状态",
}


# 中英同形或品牌词，翻译后保留拉丁字母是正常的
_KEEP_LATIN = {"telegram", "webhook", "douyin", "sparkflow", "xkb", "noVNC", "ID", "URL"}


def _tokenize(text: str):
    """按非字母数字（连字符保留）切词，并映射为 (原形, 归一化键)。"""
    return re.findall(r"[A-Za-z][A-Za-z-]*", text)


def localize(subject: str) -> tuple[str, bool]:
    """
    把英文 commit subject 本地化成中文用户向描述。

    返回 (文本, 是否纯中文)。未命中词表时返回 (原文, False)，由调用方决定
    是保留英文还是丢弃 —— 宁可输出英文原文，也不输出错译的假中文。
    """
    low = subject.lower().strip()

    # 1) 人工整句映射优先
    for en, zh in _PHRASE.items():
        if low == en:
            return zh, True

    # 2) 词表替换（长词优先，"relogin" 必须先于 "login"）
    keys = sorted(VOCAB, key=len, reverse=True)
    pieces, cursor = [], 0
    used = False
    for m in re.finditer(r"[A-Za-z][A-Za-z-]*", low):
        token = m.group(0)
        if token not in VOCAB:
            continue
        zh = VOCAB[token]
        if zh == token:      # 中英同形（Telegram/钉钉…）
            continue
        pieces.append(low[cursor:m.start()])
        pieces.append(zh)
        cursor = m.end()
        used = True
    if not used:
        return subject, False
    pieces.append(low[cursor:])
    text = "".join(pieces)

    # 3) 去掉残留英文虚词与标点，压缩空白
    text = re.sub(r"\b(?:%s)\b" % "|".join(sorted(_STOPWORDS, key=len, reverse=True)), " ", text)
    text = text.replace(" ,", ",").replace(" .", ".").replace("..", ".")
    text = re.sub(r"\s{2,}", " ", text).strip(" .,;:")

    if not text or not any("\u4e00" <= ch <= "\u9fff" for ch in text):
        return subject, False

    # 4) 质量门：拒绝中英混杂。
    # 实测反例（词表部分命中的脏输出）：
    #   "label 登录 workspace pill 状态, colour"      ← 残留 3 个英文实词
    #   "give 二维码 poll room finish say why stopped" ← 几乎整句未翻译
    # 只要还有未翻译的英文实词（≥1 个连续英文短语，或 ≥2 个孤立英文词）就丢弃该条，
    # 宁可 changelog 少一条，也不要让用户在应用中心看到半中半英的说明。
    leftovers = [
        w for w in re.findall(r"[A-Za-z][A-Za-z-]*", text)
        if w.lower() not in _STOPWORDS and w.lower() not in _KEEP_LATIN
    ]
    if leftovers:
        return subject, False
    return text, True


def collect_commits(repo: str, prev_sha: str, new_sha: str, fallback_n: int = 30):
    """
    取 prev..new 区间的 commit（旧→新）。

    prev_sha 为空（首次更新，此前从未 pin 过任何版本）时，退化为取上游最近
    fallback_n 个 commit —— 不能再拿本仓库的 git log 顶上，那是**另一个仓库**的
    SHA，跨仓库 compare 会直接失败。
    """
    if not prev_sha:
        print("  无上一版 pin，改用上游最近 "
              f"{fallback_n} 个提交作为区间")
        data = api(f"https://api.github.com/repos/{repo}/commits?per_page={fallback_n}")
        if not isinstance(data, list):
            raise RuntimeError(f"commits 返回异常: {str(data)[:200]}")
        # 只保留 new_sha 及其祖先；API 已按时间倒序，无需再按日期过滤
        return list(reversed(data))
    data = api(f"https://api.github.com/repos/{repo}/compare/{prev_sha}...{new_sha}")
    if not isinstance(data, dict) or "commits" not in data:
        raise RuntimeError(f"compare 返回异常（SHA 不存在，或两者不在同一仓库）: {str(data)[:200]}")
    return list(data["commits"])


def changelog_sections(repo: str, ref: str):
    """解析上游 CHANGELOG.md 的 (日期, [条目]) 小节。"""
    try:
        data = api(f"https://api.github.com/repos/{repo}/contents/CHANGELOG.md?ref={ref}")
    except Exception:
        return {}
    import base64
    text = base64.b64decode(data["content"]).decode("utf-8", "replace")
    out = {}
    for m in re.finditer(r"^##\s+(\d{4}-\d{2}-\d{2})\s*$\n(.*?)(?=^##\s+|\Z)",
                         text, re.M | re.S):
        items = [ln.strip("- ").strip() for ln in m.group(2).splitlines() if ln.strip().startswith("-")]
        out[m.group(1)] = items
    return out


def build_from_commits(commits):
    """commit 列表 → [(标签, 文案)]，按中文标签分组限流。"""
    buckets = {label: [] for label in set(TYPE_LABEL.values())}
    for c in reversed(commits):  # 旧→新
        message = c["commit"]["message"]
        ctype, subject = split_conventional(message)
        if not subject:
            continue
        if ctype in SKIP_TYPES:
            continue
        subject = clean_subject(subject)
        if not subject:
            continue
        # 无前缀但首字母大写（Fix xxx / Added xxx …）也算用户向
        label = TYPE_LABEL.get(ctype or "")
        if label is None:
            if not subject[0].isupper():
                continue
            label = "修复"
        # 只保留能中文化的条目；词表未覆盖的英文原文直接丢弃，
        # 避免 manifest 里出现半中半英、用户看不懂的说明。
        zh, ok = localize(subject)
        if not ok:
            continue
        buckets[label].append(zh)
    picked = []
    for label in ("新增", "修复", "优化"):  # 固定顺序，保证输出稳定
        for s in buckets.get(label, [])[-MAX_PER_TYPE:]:
            picked.append((label, s))
    return picked[:MAX_TOTAL]


def render(picked, sha) -> str:
    if not picked:
        return f"跟随上游更新（{sha[:7]}）。"
    groups = {}
    for label, s in picked:
        groups.setdefault(label, []).append(s)
    order = ["新增", "修复", "优化"]
    parts = []
    for label in order:
        if label in groups:
            body = "；".join(groups[label])
            parts.append(f"{label}：{body}")
    text = "。".join(parts) + "。"
    if len(text) > MAX_LEN:
        text = text[: MAX_LEN - 1].rstrip("；，。 ") + "…"
    return text


def set_manifest_changelog(manifest: Path, text: str) -> bool:
    """幂等写回 manifest 的 changelog 单行字段；返回是否改动。"""
    src = manifest.read_text(encoding="utf-8")
    line = f"changelog             = {text}"
    m = re.search(r"(?m)^changelog[ \t]*=.*$", src)
    if m:
        if m.group(0) == line:
            return False
        out = src[: m.start()] + line + src[m.end():]
    else:
        # 插在 checksum 之前（manifest 末尾附近），避免破坏对齐风格
        anchor = re.search(r"(?m)^checksum[ \t]*=", src)
        pos = anchor.start() if anchor else len(src)
        out = src[:pos] + line + "\n" + src[pos:]
    manifest.write_text(out, encoding="utf-8")
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="halfwaystudent/douyin-sparkflow")
    ap.add_argument("--prev-sha", default="",
                    help="上一版 pin 的上游 SHA；首次更新可留空")
    ap.add_argument("--new-sha", required=True)
    ap.add_argument("--manifest", type=Path)
    ap.add_argument("--out", type=Path, help="把 changelog 单行写到该文件")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if not re.fullmatch(r"[0-9a-f]{40}", a.new_sha):
        ap.error("--new-sha 必须是 40 位小写 commit SHA")
    if a.prev_sha and not re.fullmatch(r"[0-9a-f]{40}", a.prev_sha):
        ap.error("--prev-sha 必须是 40 位小写 commit SHA")

    print(f"拉取 {a.repo} 区间 {(a.prev_sha[:7] or '<首次>')}..{a.new_sha[:7]} …")
    commits = collect_commits(a.repo, a.prev_sha, a.new_sha)
    print(f"  区间内 {len(commits)} 个提交")
    sections = changelog_sections(a.repo, a.new_sha)
    print(f"  上游 CHANGELOG 小节: {sorted(sections)[-3:] if sections else '无'}")

    dates = sorted({c["commit"]["committer"]["date"][:10] for c in commits})
    hit = [d for d in dates if d in sections]
    picked = []
    if hit:
        # 上游手写摘要命中：它信息量最高，但原文是英文技术日志，
        # 直接塞进 manifest 违背「面向用户」要求 —— 因此只把它作为
        # 「本次确有上游功能更新」的证据，具体文案仍由 commit 本地化产出。
        print(f"  命中上游 CHANGELOG 小节 {hit[-1]}（{len(sections[hit[-1]])} 条手写摘要，"
              f"仅作校验，文案仍取 commit）")
    if not picked:
        picked = build_from_commits(commits)
        dist = {l: sum(1 for x, _ in picked if x == l) for l in ("新增", "修复", "优化")}
        print(f"  由 commit 生成 {len(picked)} 条（分布: {dist}）")

    text = render(picked, a.new_sha)
    print(f"\nchangelog（{len(text)} 字符）:\n  {text}")

    if a.dry_run:
        print("\n[dry-run] 未写任何文件")
        return 0
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(text + "\n", encoding="utf-8")
        print(f"已写入 {a.out}")
    if a.manifest:
        changed = set_manifest_changelog(a.manifest, text)
        print(f"manifest changelog: {'已更新' if changed else '无变化'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
