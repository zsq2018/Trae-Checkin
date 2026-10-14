#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按**严格口径**把签到结果推送到飞书：必须看到余额增长才算「真领到」。

为什么需要它
────────────────────────────────────────────────────────────
1) 03_checkin.py 的退出码只区分「有失败/异常」和「其他」：
   • 服务端返回 9095（今日已签）→ 退出 0
   • 9074 冷却中（本次没领到）  → 退出 0
   按退出码发通知，会把「本次根本没领到积分」说成「签到成功」。
2) claim 返回 code=0 只是服务端"受理了"，不等于积分一定进了账。
   所以再由 checkin_with_balance.py 提供签到前后的余额对比，
   **只有余额确实增加**才判为「真实领到」。

判定口径（互斥，从强到弱）
────────────────────────────────────────────────────────────
  ✅ 真实领到    成功 > 0 且 余额增加        —— 服务端受理 + 余额对得上
  ⚠️ 可疑        成功 > 0 但余额没增加/无对比数据
                                            —— code=0 但钱没到，需人工核对
  ☑️ 今日已签    成功 = 0 且 已签 > 0        —— 本次没发起领取（别处已领）
  ❌ 需要处理    待重试 > 0 / 失败 > 0 / 无汇总行
                                            —— 9074 冷却 / 鉴权失败 / 崩溃

退出码
────────────────────────────────────────────────────────────
  0 = ✅ 或 ☑️（不需要人工介入）
  1 = ⚠️ / ❌ / 飞书发送失败（让 Actions 变红，GitHub 失败邮件兜底）

用法
────────────────────────────────────────────────────────────
  FEISHU_WEBHOOK=xxx python3 notify_feishu.py --log checkin.log
  python3 notify_feishu.py --log checkin.log --delta-file .balance_delta.json --dry-run
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

TIMEOUT = 15
BEIJING_OFFSET = 8 * 3600          # 中国无夏令时，固定 UTC+8


def bj_now():
    """北京时间字符串。Actions runner 的本地时区是 UTC，
    直接 strftime 会把 UTC 当成北京时间写进通知（曾把 21:09 写成 13:09）。"""
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(time.time() + BEIJING_OFFSET))


def fmt_num(v):
    if v is None:
        return "-"
    try:
        return ("%.2f" % float(v)).rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return str(v)


def parse_summary(text):
    """返回最后的汇总 (成功, 已签, 待重试, 失败)，找不到返回 None。"""
    best = None
    for line in text.splitlines():
        if "成功" in line and "已签" in line and "失败" in line:
            nums = re.findall(r"\d+", line)
            if len(nums) >= 4:
                best = tuple(int(x) for x in nums[:4])
    return best


def parse_account_count(text):
    for line in text.splitlines():
        if "账号数量" in line:
            m = re.search(r"账号数量[:：]\s*(\d+)", line)
            if m:
                return m.group(1)
    return "?"


def evidence_lines(text, limit=5):
    """挑出能自证的关键行：服务端原始响应、余额数字、报错。
    注意排除快照/写入/分隔线这类纯过程日志 —— 它们会喧宾夺主，
    让通知看起来像贴了半篇日志。"""
    keep = ("status HTTP", "claim HTTP", "💰", "冷却", "鉴权", "Token 无效",
            "Traceback", "没有可用账号", "缺设备号")
    drop = ("📸", "🧾", "=====", "余额对比", "[余额]")
    out, seen = [], set()
    for line in text.splitlines():
        s = line.strip().strip("║").strip()
        if not s or s in seen or any(k in s for k in drop):
            continue
        if any(k in s for k in keep):
            seen.add(s)
            out.append(s)
    return out[-limit:]


def balance_summary(delta):
    """返回 (可读文案, 最差差额)。数据缺失返回 (None, None)。"""
    if not isinstance(delta, dict):
        return None, None
    rows = delta.get("rows")
    if not isinstance(rows, list) or not rows:
        return None, None
    parts, worst = [], None
    for r in rows:
        if not isinstance(r, dict):
            continue
        name = r.get("name") or "?"
        b, a, d = r.get("before"), r.get("after"), r.get("delta")
        if b is None or a is None:
            parts.append("%s：无法对比" % name)
            continue
        if d is None:
            d_txt = "?"
        elif d == 0:
            d_txt = "无变化"
        elif d > 0:
            d_txt = "+" + fmt_num(d)
        else:
            d_txt = fmt_num(d)
        parts.append("%s：%s → %s（%s）" % (name, fmt_num(b), fmt_num(a), d_txt))
        worst = d if worst is None else min(worst, d)
    if not parts:
        return None, None
    return "；".join(parts), worst


def build(log_text, delta=None):
    """返回 (icon, title, note, evidence, need_attention, summary, balance)。"""
    s = parse_summary(log_text)
    ev = evidence_lines(log_text)
    bal, worst = balance_summary(delta)

    if s is None:
        return ("❌", "Trae 签到异常：日志里没有签到汇总",
                "脚本可能中途崩溃或配置有问题，请点开运行日志查看。",
                ev or [l.strip() for l in log_text.splitlines() if l.strip()][-5:],
                True, None, bal)

    ok, already, retry, failed = s
    summary = "✅ 成功 %d   ☑️ 已签 %d   ⏳ 待重试 %d   ❌ 失败 %d" % s

    if failed > 0:
        return ("❌", "Trae 签到失败（%d 个账号出错）" % failed,
                "鉴权失败 / 异常 —— 需要重新登录或排查，见下方证据。",
                ev, True, summary, bal)
    if retry > 0:
        return ("❌", "Trae 今日没领到积分（%d 个账号被 9074 冷却）" % retry,
                "9074「当前参与用户太多」实测多是设备号或风控问题，本次未领到，"
                "明天同一时间会自动重试。",
                ev, True, summary, bal)
    if ok > 0:
        if bal is None:
            return ("⚠️", "Trae 服务端返回成功，但缺少余额对比数据",
                    "claim 返回 code=0，但没有拿到签到前后的余额，**无法确认积分真的到账**，"
                    "请人工核对一次（或看下方证据）。",
                    ev, True, summary, bal)
        if (worst or 0) > 0:
            extra = "，另有 %d 个账号今日已签" % already if already else ""
            return ("✅", "Trae 签到成功：已核对余额增加（%d 个账号）" % ok,
                    "claim 返回 code=0 且签到前后余额确实增加，积分已到账%s。" % extra,
                    ev, False, summary, bal)
        return ("⚠️", "Trae 服务端返回成功，但余额没有增加",
                "claim 返回 code=0，可是签到前后余额没有变化 —— "
                "可能是服务端记账延迟，也可能是这次领取没真正到账，**请人工核对一次**。",
                ev, True, summary, bal)
    if already > 0:
        return ("☑️", "Trae 今日已签（本次未新领积分）",
                "服务端显示今日已领取，本次**没有发起新的领取** —— "
                "积分是今天更早时候（很可能是客户端手点）领走的，脚本本身没出错。",
                ev, False, summary, bal)
    return ("❌", "Trae 签到未执行",
            "汇总行显示 0 个账号被处理，检查账号配置与 Secret。",
            ev, True, summary, bal)


def send(webhook, text):
    payload = json.dumps({"msg_type": "text", "content": {"text": text}},
                         ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(webhook, data=payload,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read().decode("utf-8", "ignore")


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default="checkin.log")
    ap.add_argument("--delta-file", default=".balance_delta.json")
    ap.add_argument("--run-url", default="")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    try:
        with open(a.log, "r", encoding="utf-8", errors="replace") as fh:
            log_text = fh.read()
    except OSError:
        log_text = ""

    delta = None
    try:
        with open(a.delta_file, "r", encoding="utf-8", errors="replace") as fh:
            delta = json.load(fh)
    except (OSError, ValueError):
        delta = None

    icon, title, note, ev, need, summary, bal = build(log_text, delta)

    lines = ["%s %s" % (icon, title),
             "时间：%s（北京时间）" % bj_now(),
             "账号数：%s" % parse_account_count(log_text)]
    if summary:
        lines.append("汇总：%s" % summary)
    if bal:
        lines.append("余额：%s" % bal)
    lines.append("说明：%s" % note)
    if ev:
        lines.append("—— 服务端证据 ——")
        lines.extend(ev)
    if a.run_url:
        lines.append("运行日志：%s" % a.run_url)
    text = "\n".join(lines)

    print("=== 将发送的内容 ===")
    print(text)
    print("=== 判定：%s（需要人工介入：%s）===" % (title, "是" if need else "否"))

    rc = 1 if need else 0

    webhook = (os.getenv("FEISHU_WEBHOOK") or "").strip()
    if a.dry_run:
        print("[dry-run] 未发送")
        return rc
    if not webhook:
        print("未配置 FEISHU_WEBHOOK，跳过发送")
        return rc
    try:
        resp = send(webhook, text)
        print("飞书返回：%s" % resp)
        if '"code":0' not in resp and '"StatusCode":0' not in resp:
            print("❌ 飞书返回不是成功码")
            return 1
    except (urllib.error.URLError, OSError, ValueError) as e:
        print("❌ 飞书发送失败：%s" % e)
        return 1
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
