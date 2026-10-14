#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按**严格口径**把 03_checkin.py 的运行结果推送到飞书。

为什么需要它
────────────────────────────────────────────────────────────
03_checkin.py 的退出码只区分「有失败/异常」和「其他」：
  • 服务端返回 9095（今日已签）→ 退出 0
  • 9074 冷却中（本次没领到）  → 退出 0
直接用退出码发通知，就会把「本次根本没领到积分」说成「签到成功」。
所以这里从日志的汇总行反推真实结果，三种口径严格互斥：

  ✅ 真实签到成功  成功 > 0        —— claim 接口返回 code=0，积分确实到账
  ☑️ 今日已签      成功 = 0 且 已签 > 0
                                  —— 服务端说今天已领过，**本次没有发起新的领取**
  ❌ 需要处理      待重试 > 0 或 失败 > 0 或 日志里找不到汇总行
                                  —— 9074 冷却 / 鉴权失败 / 脚本异常

退出码
────────────────────────────────────────────────────────────
  0 = 判定为 ✅ 或 ☑️（都不需要人工介入）
  1 = ❌ / 无法判定 / 飞书发送失败
      （让 Actions 变红，GitHub 失败邮件作为第二道兜底）

用法
────────────────────────────────────────────────────────────
  FEISHU_WEBHOOK=xxx python3 notify_feishu.py --log checkin.log
  python3 notify_feishu.py --log checkin.log --dry-run     # 只打印，不发
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

TIMEOUT = 15


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
    """挑出能自证的关键行（服务端原始响应、冷却/鉴权失败提示）。"""
    keys = ("status HTTP", "claim HTTP", "冷却", "鉴权", "Token 无效",
            "Traceback", "没有可用账号", "缺设备号")
    out, seen = [], set()
    for line in text.splitlines():
        s = line.strip().strip("║").strip()
        if not s or s in seen:
            continue
        if any(k in s for k in keys):
            seen.add(s)
            out.append(s)
    return out[-limit:]


def build(log_text, run_url):
    """返回 (icon, title, note, evidence, need_attention, summary)。"""
    s = parse_summary(log_text)
    ev = evidence_lines(log_text)
    if s is None:
        return ("❌", "Trae 签到异常：日志里没有签到汇总",
                "脚本可能中途崩溃或配置有问题，请点开运行日志查看。",
                ev or [l.strip() for l in log_text.splitlines() if l.strip()][-5:],
                True, None)

    ok, already, retry, failed = s
    summary = "✅ 成功 %d   ☑️ 已签 %d   ⏳ 待重试 %d   ❌ 失败 %d" % s

    if failed > 0:
        return ("❌", "Trae 签到失败（%d 个账号出错）" % failed,
                "鉴权失败 / 异常 —— 需要重新登录或排查，见下方证据。",
                ev, True, summary)
    if retry > 0:
        return ("❌", "Trae 今日没领到积分（%d 个账号被 9074 冷却）" % retry,
                "9074「当前参与用户太多」实测多是设备号或风控问题，本次未领到，"
                "明天同一时间会自动重试。",
                ev, True, summary)
    if ok > 0:
        extra = "，另有 %d 个账号今日已签" % already if already else ""
        return ("✅", "Trae 签到成功：本次真实领到积分（%d 个账号）" % ok,
                "claim 接口返回 code=0，积分已到账%s。" % extra,
                ev, False, summary)
    if already > 0:
        return ("☑️", "Trae 今日已签（本次未新领积分）",
                "服务端显示今日已领取，本次**没有发起新的领取** —— "
                "积分是今天更早时候（很可能是客户端手点）领走的，脚本本身没出错。",
                ev, False, summary)
    return ("❌", "Trae 签到未执行",
            "汇总行显示 0 个账号被处理，检查账号配置与 Secret。",
            ev, True, summary)


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
    ap.add_argument("--run-url", default="")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    try:
        with open(a.log, "r", encoding="utf-8", errors="replace") as fh:
            log_text = fh.read()
    except OSError:
        log_text = ""

    icon, title, note, ev, need, summary = build(log_text, a.run_url)

    lines = ["%s %s" % (icon, title),
             "时间：%s（北京时间）" % __import__("time").strftime("%Y-%m-%d %H:%M"),
             "账号数：%s" % parse_account_count(log_text)]
    if summary:
        lines.append("汇总：%s" % summary)
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
