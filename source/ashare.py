#!/usr/bin/env python3
"""Free, bounded-watchlist A-share morning rules brief; no trading or AI calls."""
from __future__ import annotations
import argparse
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import sys

import digest as d

SOURCE = 'https://qt.gtimg.cn/q='
INDICES = ('sh000001', 'sz399001', 'sz399006')
# A transparent finite sample across sectors, NOT a whole-market or personalized screen.
WATCHLIST = ('sh600519', 'sz000858', 'sh601318', 'sh600036', 'sh601398',
             'sh600900', 'sh601088', 'sh601857', 'sh600028', 'sh601012',
             'sz300750', 'sz002594', 'sz000333', 'sh600030', 'sh600276',
             'sh688981', 'sz002415', 'sh600309', 'sh601668', 'sh600406')
CALENDAR = Path(__file__).with_name('config') / 'ashare_calendar.json'
MAX_AGE = 180


@dataclass(frozen=True)
class Quote:
    symbol: str
    name: str
    price: float
    previous: float
    open: float
    change: float
    high: float
    low: float
    volume_raw: float
    amount_wan: float
    volume_ratio: float | None
    at: datetime


def trading_day(day, calendar):
    if calendar.get('schema') != 1 or calendar.get('year') != day.year:
        raise d.SafeError('交易日历未覆盖当前年份，已停止；请先更新官方休市日历。')
    try:
        start, end = date.fromisoformat(calendar['valid_from']), date.fromisoformat(calendar['valid_until'])
        holidays = {date.fromisoformat(v) for v in calendar['closed_dates']}
        if not start <= day <= end or any(v.year != day.year for v in holidays):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise d.SafeError('交易日历格式或有效期无效，已停止。') from None
    return day.weekday() < 5 and day not in holidays


def in_window(now):
    local = now.astimezone(d.SHANGHAI)
    return time(10, 0) <= local.time().replace(tzinfo=None) <= time(10, 20)


def numeric(value):
    n = float(value)
    if not math.isfinite(n):
        raise ValueError
    return n


def parse_quotes(text, symbols, now):
    """Tencent zero-based fields; turnover amount is 10,000 CNY.

    Volume is kept raw: units vary by instrument (STAR board uses shares).
    It is only checked for positivity, never used as a normalized ranking.

    Each invalid/stale security is omitted. Mandatory index coverage is enforced
    by build_payload. No cached quote or zero-price fallback is ever used.
    """
    parsed = {}
    for symbol, raw in re.findall(r'v_((?:sh|sz)\d{6})="([^"\r\n]*)";', text):
        if symbol not in symbols:
            continue
        if symbol in parsed:
            raise d.SafeError('行情返回重复证券，已停止。')
        f = raw.split('~')
        try:
            if len(f) < 50 or f[2] != symbol[2:]:
                raise ValueError
            at = datetime.strptime(f[30], '%Y%m%d%H%M%S').replace(tzinfo=d.SHANGHAI)
            age = (now - at).total_seconds()
            if not 0 <= age <= MAX_AGE or at.date() != now.astimezone(d.SHANGHAI).date():
                raise ValueError
            price, previous, opening = map(numeric, (f[3], f[4], f[5]))
            change, high, low = map(numeric, (f[32], f[33], f[34]))
            volume, amount = map(numeric, (f[36], f[57] if len(f) > 57 and f[57] else f[37]))
            delta = numeric(f[31])
            ratio = numeric(f[49]) if f[49] and numeric(f[49]) != 0 else None
            if (min(price, previous, opening, high, low) <= 0 or volume <= 0 or amount <= 0
                    or not low <= price <= high or not low <= opening <= high
                    or abs(price - previous - delta) > .03
                    or abs((price / previous - 1) * 100 - change) > .08
                    or abs(change) > 45 or ratio is not None and ratio < 0):
                raise ValueError
            name = d.clean(f[1])[:20]
            if not name:
                raise ValueError
            parsed[symbol] = Quote(symbol, name, price, previous, opening, change, high, low,
                                    volume, amount, ratio, at)
        except (ValueError, IndexError, OverflowError):
            continue
    return parsed


def screen(quotes):
    benchmark = quotes[INDICES[0]].change
    eligible = []
    for symbol in WATCHLIST:
        q = quotes.get(symbol)
        if q is None or any(x in q.name.upper() for x in ('ST', '退', 'N', 'C')):
            continue
        if (0.5 <= q.change <= 5 and q.price >= q.open and q.amount_wan >= 10000
                and q.volume_ratio is not None and q.volume_ratio >= 1.2):
            score = q.change - benchmark + min(q.volume_ratio, 3)
            eligible.append((score, q))
    return [q for _, q in sorted(eligible, key=lambda x: (-x[0], x[1].symbol))[:5]]


def build_payload(text, now, calendar, *, fixture=False):
    local = now.astimezone(d.SHANGHAI)
    base = {'schema': 1, 'source': SOURCE, 'date': local.date().isoformat(),
            'fetched_at': now.isoformat(), 'fixture': fixture}
    if not trading_day(local.date(), calendar):
        return {**base, 'status': 'skip', 'reason': '交易所休市，跳过。'}
    if not in_window(now):
        return {**base, 'status': 'skip', 'reason': '超出北京时间10:00–10:20窗口，跳过晚到任务，不补发。'}
    quotes = parse_quotes(text, INDICES + WATCHLIST, now)
    if any(s not in quotes for s in INDICES):
        raise d.SafeError('指数行情缺失、过期或校验失败，未生成股票结论。')
    coverage = sum(s in quotes for s in WATCHLIST)
    if coverage < math.ceil(len(WATCHLIST) * .8):
        raise d.SafeError('观察池有效行情不足80%，未生成股票结论。')
    indices = [quotes[s] for s in INDICES]
    candidates = screen(quotes)
    if all(q.change >= .3 for q in indices):
        scenario = '指数同步偏强。若继续守住今开且量能维持，关注强势延续；跌回今开下方则削弱此情景。'
    elif all(q.change <= -.3 for q in indices):
        scenario = '指数同步偏弱。若不能收复今开，继续防范回落；重新站回今开才考虑修复情景。'
    else:
        scenario = '指数分化或方向尚不明确。观察能否共同站稳今开；冲高回落时不追涨。'
    blocks = [f'A股早盘量价观察｜{local:%Y-%m-%d %H:%M} 北京时间',
              '免费公开行情规则简报，不是完整研究或全市场推荐。',
              '指数快照：\n' + '\n'.join(f'{q.name} {q.price:.2f}（{q.change:+.2f}%），成交{q.amount_wan/10000:.1f}亿元' for q in indices),
              '情景：' + scenario,
              f'有限观察池：固定{len(WATCHLIST)}只，当前有效{coverage}只。筛选：涨幅0.5%–5%、不低于今开、成交≥1亿元、量比≥1.2。按相对上证涨幅+量比（上限3）排序，最多5只。']
    if candidates:
        blocks.append('观察名单（不足3只不凑数）：\n' + '\n'.join(
            f'{i}. {q.name} {q.symbol[2:]}｜{q.price:.2f}元，{q.change:+.2f}%，成交{q.amount_wan/10000:.2f}亿元，腾讯量比{q.volume_ratio:.2f}\n'
            f'观察条件：继续守住今开{q.open:.2f}；跌破时规则转弱。日内高点{q.high:.2f}附近注意冲高回落。'
            for i, q in enumerate(candidates, 1)))
    else:
        blocks.append('当前没有满足全部条件的观察股，保持空名单。')
    used = indices + candidates
    blocks.append(f'行情时间：{min(q.at for q in used):%H:%M:%S}–{max(q.at for q in used):%H:%M:%S}（北京时间）；来源：腾讯财经公开行情 https://qt.gtimg.cn/ 。最多接受3分钟延迟，非交易所直连。')
    blocks.append('风险：量比与短时涨幅可能失真，早盘信号会反转；本规则不含新闻、财报或估值核验。仅供观察，不构成个性化投资建议或买卖指令，不保证收益。')
    message = '\n\n'.join(blocks)
    if d.utf16_length(message) > 3900:
        raise d.SafeError('简报超出单条消息限制，已停止。')
    return {**base, 'status': 'ready', 'quote_times': [q.at.isoformat() for q in used],
            'coverage': coverage, 'candidate_symbols': [q.symbol for q in candidates], 'message': message}


def validate_send(payload, now, calendar, env):
    if env.get('ASHARE_ENABLED') != 'true':
        raise d.SafeError('A股发送未开启。')
    if (payload.get('schema') != 1 or payload.get('source') != SOURCE or payload.get('fixture') is not False
            or payload.get('status') != 'ready'):
        raise d.SafeError('拒绝发送演示数据、跳过状态或未知格式。')
    if not trading_day(now.astimezone(d.SHANGHAI).date(), calendar) or not in_window(now):
        raise d.SafeError('非交易日或已超出早盘发送窗口，停止。')
    age = (now - d.parse_time(payload.get('fetched_at'))).total_seconds()
    if not 0 <= age <= 120 or payload.get('date') != now.astimezone(d.SHANGHAI).date().isoformat():
        raise d.SafeError('简报已过期，停止。')
    stamps = payload.get('quote_times')
    if not isinstance(stamps, list) or len(stamps) < 3 or any(not 0 <= (now-d.parse_time(t)).total_seconds() <= MAX_AGE for t in stamps):
        raise d.SafeError('行情在发送前已过期，停止。')
    message = payload.get('message')
    if not isinstance(message, str) or not message.strip() or d.utf16_length(message) > 3900:
        raise d.SafeError('简报内容为空或过长。')
    if not re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]{20,}', env.get('TELEGRAM_BOT_TOKEN', '')):
        raise d.SafeError('Telegram bot secret 缺失或格式无效。')
    if not re.fullmatch(r'[1-9][0-9]*', env.get('TELEGRAM_CHAT_ID', '')):
        raise d.SafeError('接收人必须是已核对的私人聊天ID。')


def deliver(payload, calendar, state_dir, now=None):
    now = now or datetime.now(timezone.utc)
    validate_send(payload, now, calendar, os.environ)
    kind, cycle = d.guard_github(payload['date'], schedule_hour=10, now=now, with_cycle=True)
    if kind != 'scheduled':
        raise d.SafeError('此功能手动运行仅预览；真实发送只允许定时任务。')
    root = Path(state_dir)
    root.mkdir(parents=True, exist_ok=True)
    path = root / (cycle + '.ashare.json')
    try:
        with path.open('x', encoding='utf-8') as f:
            json.dump({'cycle': cycle, 'status': 'reserved'}, f)
    except FileExistsError:
        raise d.SafeError('本周期已有发送记录，不盲目重试。') from None
    # Revalidate after history lookup; never deliver a quote that expired meanwhile.
    validate_send(payload, datetime.now(timezone.utc), calendar, os.environ)
    d.send_message(os.environ['TELEGRAM_BOT_TOKEN'], os.environ['TELEGRAM_CHAT_ID'], payload['message'])
    path.write_text(json.dumps({'cycle': cycle, 'status': 'confirmed'}), encoding='utf-8')
    print('Telegram API 已确认1条A股观察简报；不代表用户已阅读。')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('build', 'send'))
    parser.add_argument('--input-quotes', type=Path, help='UTF-8 fixture; never sendable')
    parser.add_argument('--out', type=Path, default=Path('out/ashare'))
    args = parser.parse_args(argv)
    try:
        calendar = d.load_json(CALENDAR)
        now = datetime.now(timezone.utc)
        if args.command == 'build':
            # Gate before the request, including unknown calendar years.
            eligible = trading_day(now.astimezone(d.SHANGHAI).date(), calendar) and in_window(now)
            text = ''
            if eligible:
                text = (args.input_quotes.read_text(encoding='utf-8') if args.input_quotes else
                        d.get_bytes(SOURCE + ','.join(INDICES + WATCHLIST), limit=500000).decode('gb18030'))
            now = datetime.now(timezone.utc) # Timestamp after the network response, not before it.
            payload = build_payload(text, now, calendar, fixture=bool(args.input_quotes))
            args.out.mkdir(parents=True, exist_ok=True)
            (args.out/'brief.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
            (args.out/'preview.txt').write_text(payload.get('message', payload.get('reason', '')), encoding='utf-8')
            print('预览已生成；未发送。状态：' + payload['status'])
        else:
            payload = d.load_json(args.out/'brief.json')
            if payload.get('status') == 'skip':
                print('休市或超时，未发送。')
            else:
                deliver(payload, calendar, 'delivery-state/ashare')
        return 0
    except d.SafeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception:
        print('A股简报处理失败；不会发送未经校验的数据或自动重试消息。', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
