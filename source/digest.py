#!/usr/bin/env python3
"""Dependency-free GitHub Trending preview and explicitly gated Telegram delivery."""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

SOURCE = "https://github.com/trending?since=daily"
SHANGHAI = ZoneInfo("Asia/Shanghai")
MAX_HTML = 5_000_000
MAX_MESSAGE = 3900  # Conservative UTF-16 units; Telegram allows 4096 characters.
DELIVERY_STEP = "Delivery attempt (do not rename)"
REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")


class SafeError(Exception):
    """Only fixed, non-secret error messages may be shown to users/logs."""


@dataclass
class Node:
    tag: str
    attrs: dict = field(default_factory=dict)
    children: list = field(default_factory=list)

    def text(self):
        return "".join(c if isinstance(c, str) else c.text() for c in self.children)

    def walk(self, tag=None):
        for child in self.children:
            if isinstance(child, Node):
                if tag is None or child.tag == tag:
                    yield child
                yield from child.walk(tag)


class Document(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("root")
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, dict(attrs))
        self.stack[-1].children.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def clean(text):
    # Strip control and directional override characters from untrusted descriptions.
    return " ".join("".join(" " if c.isspace() else c for c in text if c.isspace() or not unicodedata.category(c).startswith("C")).split())


@dataclass(frozen=True)
class Project:
    repository: str
    description: str
    stars_today: int
    language: str

    @property
    def url(self):
        return "https://github.com/" + self.repository


def parse_trending(html):
    doc = Document()
    doc.feed(html)
    projects = []
    seen = set()
    for article in doc.root.walk("article"):
        if "Box-row" not in article.attrs.get("class", "").split():
            continue
        headings = list(article.walk("h2"))
        if len(headings) != 1:
            raise SafeError("Trending 页面结构变化：项目标题无法确认，已停止。")
        links = list(headings[0].walk("a"))
        if len(links) != 1:
            raise SafeError("Trending 页面结构变化：项目链接无法确认，已停止。")
        href = links[0].attrs.get("href", "")
        repo = href[1:] if href.startswith("/") else ""
        if not REPO_RE.fullmatch(repo) or repo.lower() in seen:
            raise SafeError("Trending 项目链接无效或重复，已停止。")
        seen.add(repo.lower())
        values = []
        for span in article.walk("span"):
            if "float-sm-right" in span.attrs.get("class", "").split():
                match = re.fullmatch(r"([0-9]+(?:,[0-9]{3})*) stars? today", clean(span.text()))
                if match:
                    values.append(int(match.group(1).replace(",", "")))
        if len(values) != 1:
            raise SafeError("某项目缺少明确的 stars today；不会用总 stars 或 0 替代，已停止。")
        paragraphs = list(article.walk("p"))
        description = clean(paragraphs[0].text()) if paragraphs else ""
        languages = [clean(n.text()) for n in article.walk("span") if n.attrs.get("itemprop") == "programmingLanguage"]
        projects.append(Project(repo, description, values[0], languages[0] if languages else "未标注"))
    if not projects:
        raise SafeError("Trending 未解析出项目，可能被限流或页面已变化；不发送空榜。")
    # The source is Trending's candidate list, not all repositories on GitHub.
    return sorted(projects, key=lambda p: (-p.stars_today, p.repository.lower()))[:20]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SafeError("服务返回重定向；已停止，未向其他地址转发凭据。")


OPENER = urllib.request.build_opener(NoRedirect)


def get_bytes(url, headers=None, limit=MAX_HTML):
    """Retry read-only GETs only. Never expose exception URLs or response bodies."""
    headers = {"User-Agent": "telegram-github-digest/1.0", **(headers or {})}
    for attempt in range(3):
        try:
            with OPENER.open(urllib.request.Request(url, headers=headers), timeout=25) as response:
                data = response.read(limit + 1)
                if len(data) > limit:
                    raise SafeError("远端响应过大，已停止。")
                return data
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise SafeError("只读请求失败，请检查 GitHub 服务状态或权限。") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt == 2:
                raise SafeError("只读请求连接失败，已停止。") from None
        time.sleep(2 ** attempt)
    raise SafeError("只读请求失败。")


def utf16_length(text):
    return len(text.encode("utf-16-le")) // 2


def clip(text, limit=600):
    if utf16_length(text) <= limit:
        return text
    result = ""
    for char in text:
        if utf16_length(result + char + "…") > limit:
            break
        result += char
    return result + "…"


def chunk_blocks(blocks, limit=MAX_MESSAGE):
    # Split long individual blocks first. Do not break surrogate pairs.
    pieces = []
    for block in blocks:
        current, size = "", 0
        for char in block:
            units = utf16_length(char)
            if size + units > limit:
                pieces.append(current)
                current, size = "", 0
            current += char
            size += units
        if current:
            pieces.append(current)
    chunks, current = [], ""
    for piece in pieces:
        candidate = current + ("\n\n" if current else "") + piece
        if utf16_length(candidate) > limit:
            chunks.append(current)
            current = piece
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def description_hash(description):
    return hashlib.sha256(description.encode("utf-8")).hexdigest()


def make_payload(projects, now, overrides=None, fixture=False):
    overrides = overrides or {}
    date = now.astimezone(SHANGHAI).date().isoformat()
    blocks = [f"GitHub Trending 日报 · {date}\n全语言候选榜，按页面 stars today 降序，最多 20 个；本次 {len(projects)} 个。\n抓取时间：{now.astimezone(SHANGHAI):%Y-%m-%d %H:%M}（北京时间）\n{SOURCE}\n不是 GitHub 全站新增 stars 排名；today 采用 GitHub 页面口径。"]
    if fixture:
        blocks[0] = "【离线演示数据，不能发送】\n" + blocks[0]
    complete = True
    for rank, p in enumerate(projects, 1):
        override = overrides.get(p.repository, {})
        valid = (isinstance(override, dict)
                 and override.get("description_sha256") == description_hash(p.description)
                 and isinstance(override.get("zh"), str)
                 and re.search(r"[\u3400-\u9fff]", override["zh"]))
        if valid:
            summary = "中文简介（已提供译文）：" + clean(override["zh"])
        elif p.description:
            complete = False
            summary = "原文简介：" + p.description
        else:
            summary = "简介：项目未提供简介。"
        blocks.append(f"{rank}. {p.repository}\n今日新增 ★ {p.stars_today:,} · {p.language}\n{clip(summary)}\n{p.url}")
    if not complete:
        blocks.append("说明：项目简介按用户选择保留原文。")
    chunks = chunk_blocks(blocks, MAX_MESSAGE - 60)
    chunks = [f"[{i}/{len(chunks)}]\n{c}" for i, c in enumerate(chunks, 1)]
    return {"schema": 1, "source": SOURCE, "date": date, "fetched_at": now.isoformat(),
            "fixture": fixture, "chinese_complete": bool(complete),
            "projects": [{**asdict(p), "description_sha256": description_hash(p.description)} for p in projects],
            "messages": chunks}


def load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise SafeError("本地 JSON 无法读取或解析。") from None


def parse_time(value):
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise ValueError
        return dt
    except (AttributeError, TypeError, ValueError):
        raise SafeError("时间字段无效，已停止。") from None


def github_get(path):
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise SafeError("缺少 Actions 自动提供的只读 GITHUB_TOKEN，无法检查重复发送。")
    try:
        return json.loads(get_bytes("https://api.github.com" + path, {
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"}, limit=3_000_000))
    except (ValueError, UnicodeError):
        raise SafeError("GitHub API 响应无效，无法检查重复发送。") from None


def delivery_class(event):
    if event == "schedule":
        return "scheduled"
    if event == "workflow_dispatch":
        return "manual-test"
    raise SafeError("运行事件未知；无法确认去重类别，已停止。")


def scheduled_cycle(at, hour=20):
    """Most recent Shanghai schedule boundary, not the quote/fetch date.

    GitHub does not expose the intended cron occurrence. Bind to run.created_at
    so queueing and reruns cannot move a reservation. Delays of >=24 hours
    cannot be reconstructed; this is a conservative observable cycle policy.
    """
    local = at.astimezone(SHANGHAI)
    boundary = local.replace(hour=hour, minute=0, second=0, microsecond=0)
    return (local.date() if local >= boundary else (local - timedelta(days=1)).date()).isoformat()


def guard_github(date, env=None, get=None, *, schedule_hour=20, now=None, with_cycle=False):
    """An earlier delivery step reserves its run cycle within its event class.

    Serialized workflow + read-only history avoids repository write permissions.
    A started but failed/cancelled step may already have delivered, so block it.
    """
    env = os.environ if env is None else env
    get = github_get if get is None else get
    if env.get("GITHUB_ACTIONS") != "true" or env.get("GITHUB_RUN_ATTEMPT") != "1":
        raise SafeError("发送仅允许 Actions 的首次运行；不要盲目重跑可能已经发送的任务。")
    repo, run_id = env.get("GITHUB_REPOSITORY", ""), env.get("GITHUB_RUN_ID", "")
    if not REPO_RE.fullmatch(repo) or not run_id.isdigit():
        raise SafeError("Actions 上下文不完整，无法检查重复发送。")
    base = f"/repos/{repo}/actions"
    current = get(f"{base}/runs/{run_id}")
    kind = delivery_class(current.get("event"))
    if current.get("event") != env.get("GITHUB_EVENT_NAME"):
        raise SafeError("运行事件与 Actions 上下文不一致，已停止。")
    cycle = scheduled_cycle(parse_time(current.get("created_at")), schedule_hour) if kind == "scheduled" else date
    if now is not None and kind == "scheduled" and cycle != scheduled_cycle(now, schedule_hour):
        raise SafeError("运行排队跨越计划周期，已停止，避免占用下一周期或重复投递。")
    workflow_id = current.get("workflow_id")
    if not isinstance(workflow_id, int):
        raise SafeError("无法确认当前 workflow。")
    # Recent run window exceeds this workflow's 8-minute execution timeout.
    # Old reruns are disallowed, including reruns created outside this window.
    start = (datetime.fromisoformat(date).replace(tzinfo=SHANGHAI) - timedelta(days=2)).astimezone(timezone.utc)
    query = urllib.parse.urlencode({"per_page": 100, "created": ">=" + start.strftime("%Y-%m-%dT%H:%M:%SZ")})
    data = get(f"{base}/workflows/{workflow_id}/runs?{query}")
    runs = data.get("workflow_runs")
    if (not isinstance(runs, list) or data.get("total_count", 101) >= 100
            or data.get("total_count") != len(runs)):
        raise SafeError("运行历史过多或不完整；为避免重复，已停止。")
    if any(not isinstance(r, dict) for r in runs):
        raise SafeError("运行历史包含无效记录，已停止。")
    if not any(str(r.get("id")) == run_id for r in runs):
        raise SafeError("运行历史尚未同步；为避免重复，已停止。")
    for run in runs:
        other_kind = delivery_class(run.get("event"))
        other_id = run.get("id")
        if str(other_id) == run_id:
            if other_kind != kind:
                raise SafeError("当前运行历史的去重类别不一致，已停止。")
            continue
        if not isinstance(other_id, int):
            raise SafeError("运行历史包含无效记录，已停止。")
        # Read all attempts, not just the latest, which could be a skipped rerun.
        jobs_data = get(f"{base}/runs/{other_id}/jobs?filter=all&per_page=100")
        jobs = jobs_data.get("jobs")
        if (not isinstance(jobs, list) or jobs_data.get("total_count", 101) >= 100
                or jobs_data.get("total_count") != len(jobs)):
            raise SafeError("任务历史不完整；为避免重复，已停止。")
        for job in jobs:
            steps = job.get("steps")
            if not isinstance(steps, list):
                raise SafeError("步骤历史不完整；为避免重复，已停止。")
            for step in steps:
                if step.get("name") != DELIVERY_STEP or step.get("conclusion") == "skipped":
                    continue
                started = step.get("started_at")
                if not started and step.get("status") != "queued":
                    raise SafeError("发送步骤缺少时间记录；为避免重复，已停止。")
                # Validate even the other event class; incomplete history fails closed.
                if started:
                    step_time = parse_time(started)
                    other_cycle = (scheduled_cycle(parse_time(run.get("created_at")), schedule_hour)
                                   if other_kind == "scheduled" else step_time.astimezone(SHANGHAI).date().isoformat())
                else:
                    other_cycle = None
                # Legacy code could send after its creation cycle ended. Its
                # actual delivery cycle also remains reserved conservatively.
                actual_cycle = (scheduled_cycle(step_time, schedule_hour)
                                if started and other_kind == "scheduled" else other_cycle)
                if cycle in (other_cycle, actual_cycle) and other_kind == kind:
                    raise SafeError("本计划周期已有同类发送步骤启动，可能已送达或部分送达；已拦截重复发送，请先检查 Telegram。")
    return (kind, cycle) if with_cycle else kind


def validate_delivery(payload, now, env=None):
    env = os.environ if env is None else env
    if env.get("DIGEST_ENABLED") != "true":
        raise SafeError("正式发送未开启。请先配置 secrets 并核对私人接收人。")
    mode = env.get("DIGEST_LANGUAGE_MODE", "original-approved")
    if mode not in ("chinese-required", "original-approved"):
        raise SafeError("简介语言模式配置无效。")
    if not isinstance(payload, dict) or payload.get("schema") != 1 or payload.get("source") != SOURCE or payload.get("fixture") is not False:
        raise SafeError("拒绝发送演示数据或未知格式。")
    fetched = parse_time(payload.get("fetched_at"))
    age = (now - fetched).total_seconds()
    if age < -60 or age > 1800 or payload.get("date") != now.astimezone(SHANGHAI).date().isoformat():
        raise SafeError("日报已过期或日期不符，请重新生成预览。")
    if mode == "chinese-required" and payload.get("chinese_complete") is not True:
        raise SafeError("完整中文摘要仍未就绪；已拦截正式发送。")
    messages = payload.get("messages")
    if not isinstance(messages, list) or not 1 <= len(messages) <= 30:
        raise SafeError("消息列表无效。")
    if any(not isinstance(m, str) or not m.strip() or utf16_length(m) > 4096 for m in messages):
        raise SafeError("消息为空或超过 Telegram 长度限制。")
    token, chat_id = env.get("TELEGRAM_BOT_TOKEN", ""), env.get("TELEGRAM_CHAT_ID", "")
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{20,}", token):
        raise SafeError("Telegram bot secret 缺失或格式无效。")
    # Private chat only. A group or public channel would change the audience.
    if not re.fullmatch(r"[1-9][0-9]*", chat_id):
        raise SafeError("接收人必须为已核对的私人聊天正整数 ID。")


def send_message(token, chat_id, text, opener=None):
    """Exactly one attempt. Even HTTP errors are not retried automatically."""
    opener = OPENER if opener is None else opener
    body = json.dumps({"chat_id": chat_id, "text": text,
                       "link_preview_options": {"is_disabled": True}}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request("https://api.telegram.org/bot" + token + "/sendMessage",
                                     data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with opener.open(request, timeout=25) as response:
            result = json.loads(response.read(1_000_001))
        if result.get("ok") is not True or not isinstance(result.get("result", {}).get("message_id"), int):
            raise SafeError("Telegram 未确认成功；可能已送达，不会自动重试，请先检查聊天。")
    except SafeError:
        raise
    except Exception:
        # urllib HTTPError/URLError and JSON exceptions may contain the token URL.
        raise SafeError("Telegram 发送结果不确定或被拒绝；不会自动重试，请先检查聊天。") from None


def deliver(payload, state_dir, now=None):
    now = now or datetime.now(timezone.utc)
    validate_delivery(payload, now)
    kind, cycle = guard_github(payload["date"], now=now, with_cycle=True)
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    if (state_dir / (payload["date"] + ".json")).exists():
        raise SafeError("本地已有未分类的当日发送记录；请先核对收件情况。")
    state_path = state_dir / (cycle + "." + kind + ".json")
    state = {"date": payload["date"], "cycle": cycle, "delivery_class": kind, "status": "reserved", "confirmed_chunks": 0,
             "total_chunks": len(payload["messages"])}
    try:
        with state_path.open("x", encoding="utf-8") as f:
            json.dump(state, f)
    except FileExistsError:
        raise SafeError("本地已有当日发送记录；请先核对收件情况。") from None
    for index, message in enumerate(payload["messages"]):
        # Stop rather than let a day-boundary split evade the date guard.
        if datetime.now(timezone.utc).astimezone(SHANGHAI).date().isoformat() != payload["date"]:
            raise SafeError("发送跨越北京时间午夜，已停止剩余分段，请核对收件情况。")
        if kind == "scheduled" and scheduled_cycle(datetime.now(timezone.utc)) != cycle:
            raise SafeError("发送跨越计划周期，已停止剩余分段，请核对收件情况。")
        state["status"] = "sending"
        state_path.write_text(json.dumps(state), encoding="utf-8")
        send_message(os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"], message)
        state["confirmed_chunks"] = index + 1
        state["status"] = "complete" if index + 1 == len(payload["messages"]) else "partial"
        state_path.write_text(json.dumps(state), encoding="utf-8")
        if index + 1 < len(payload["messages"]):
            time.sleep(1.2)
    print(f"Telegram 已确认 {len(payload['messages'])} 个分段；请在聊天中核对。")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="只生成文件，不发送；默认抓取实时榜单")
    build.add_argument("--input-html", type=Path, help="离线 fixture；生成结果不能发送")
    build.add_argument("--overrides", type=Path, default=Path("chinese_overrides.json"))
    build.add_argument("--out", type=Path, default=Path("out"))
    send = commands.add_parser("send", help="仅限完成配置并授权后的 Actions 正式发送")
    send.add_argument("--payload", type=Path, default=Path("out/digest.json"))
    send.add_argument("--state-dir", type=Path, default=Path("delivery-state"))
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            html = args.input_html.read_text(encoding="utf-8") if args.input_html else get_bytes(SOURCE, {"Accept-Language": "en"}).decode("utf-8")
            overrides = load_json(args.overrides) if args.overrides.exists() else {}
            if not isinstance(overrides, dict):
                raise SafeError("中文覆写文件必须是 JSON 对象。")
            payload = make_payload(parse_trending(html), datetime.now(timezone.utc), overrides, bool(args.input_html))
            args.out.mkdir(parents=True, exist_ok=True)
            (args.out / "digest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            (args.out / "preview.txt").write_text("\n\n" + ("\n\n" + "=" * 30 + "\n\n").join(payload["messages"]) + "\n", encoding="utf-8")
            print(f"预览已生成：{len(payload['projects'])} 个项目，{len(payload['messages'])} 个分段。简介保留原文（可选人工覆写）。未发送。")
        else:
            deliver(load_json(args.payload), args.state_dir)
        return 0
    except SafeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception:
        # Never dump raw network exceptions, local config, payload or credentials.
        print("处理失败；未自动重试发送。请检查输入、配置和服务状态。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

