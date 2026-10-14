#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""带「余额前后对比」的签到入口（取代直接跑 03_checkin.py）。

为什么要它
────────────────────────────────────────────────────────────
claim 接口返回 code=0 只能说明"服务端受理了这次领取"，用户要的是**真到账**的
证据。所以这里在签到**前后各读一次积分余额**，把差额写进 .balance_delta.json，
再由 notify_feishu.py 附到飞书通知里：

    余额：531.26 → 631.26（+100）    ← 真到账的硬证据
    余额：531.26 → 531.26（未变化）  ← 服务端说成功但没加，判为需要人工核对

判定归 notify_feishu.py 做，本脚本只负责「量」和落盘。

⚠️ 本脚本会调用真实接口，只能在持有 token 轮换链的那一处跑（GitHub Actions）。
   本机不要跑 —— 同一份 refreshToken 在两处用会互踢，本机的 accounts.json 已
   改名为 .bak 就是为了防这个。

用法
────────────────────────────────────────────────────────────
  python3 checkin_with_balance.py            # Actions 里这么跑
"""

import io
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import trae_core as T  # noqa: E402

DELTA_FILE = os.path.join(HERE, ".balance_delta.json")
REREAD_WAIT = 5          # 余额未变时的重读间隔（秒）
REREAD_TIMES = 3         # 重读次数


class Tee(io.TextIOBase):
    """同时写到真实 stdout 和缓冲区：日志照常被 workflow tee 走，
    同时留一份文本给我们解析「成功 N」的汇总行。"""

    def __init__(self, *streams):
        self.streams = streams

    def write(self, s):
        for st in self.streams:
            st.write(s)
        return len(s)

    def flush(self):
        for st in self.streams:
            st.flush()


def credits_detail(acc, device_id):
    """返回 (总额, 已用, 剩余)，与 04_credit_monitor.py 同一口径。"""
    headers = T.ug_headers(acc["accessToken"], device_id, acc.get("region"))
    body = '{"require_usage": true, "full_data": true}'
    try:
        status, d, _ = T.http_post(T.EP_ENTITLE, headers, body, 20)
    except Exception:                            # noqa: BLE001
        status, d = 500, None
    if isinstance(d, dict) and status < 400:
        root = d.get("data") if isinstance(d.get("data"), dict) else d
        us = T.pick(root, "usage_summary", "usageSummary")
        if isinstance(us, dict):
            total, used = us.get("total_amount"), us.get("consumed_amount")
            if total is not None and used is not None:
                try:
                    total, used = float(total), float(used)
                    return total, used, round(total - used, 2)
                except (TypeError, ValueError):
                    pass
    remain = T.api_usage(acc, device_id)
    if remain is not None:
        return None, None, remain
    return None


def snapshot(tag):
    """读一次所有账号的余额。不抛异常，失败记在 error 字段里。"""
    print("\n📸 [余额] %s快照" % tag)
    out = {}
    try:
        accounts = T.load_accounts()
    except Exception as e:                       # noqa: BLE001
        print("   ❌ 读取账号失败: %s" % e)
        return out
    for acc in accounts:
        key = T._acct_key(acc)
        name = acc.get("name") or acc.get("uid") or key
        rec = {"name": name}
        try:
            ok, msg = T.ensure_token(acc)
            if not ok:
                rec["error"] = "凭证失效：%s" % msg
            else:
                device_id, _src = T.resolve_device_id(acc)
                acc["deviceId"] = device_id
                d = credits_detail(acc, device_id)
                if d is None:
                    rec["error"] = "查询失败"
                else:
                    rec["total"], rec["used"], rec["remain"] = d
        except Exception as e:                   # noqa: BLE001
            rec["error"] = str(e)
        if rec.get("error"):
            print("   ❌ %s：%s" % (name, rec["error"]))
        else:
            print("   💰 %s：总额 %s · 已用 %s · 剩余 %s"
                  % (name, T._fmt(rec.get("total")), T._fmt(rec.get("used")),
                     T._fmt(rec["remain"])))
        out[key] = rec
    return out


def claimed_count(text):
    """从签到日志的汇总行取「成功」数：只有 claim 返回 code=0 才算。"""
    n = 0
    for line in text.splitlines():
        if "成功" in line and "已签" in line and "失败" in line:
            m = re.findall(r"\d+", line)
            if len(m) >= 4:
                n = int(m[0])
    return n


def moved(before, after):
    for k, b in before.items():
        a = after.get(k) or {}
        if b.get("remain") is not None and a.get("remain") is not None \
                and a["remain"] > b["remain"]:
            return True
    return False


def main():
    before = snapshot("签到前")

    buf = io.StringIO()
    real = sys.stdout
    sys.stdout = Tee(real, buf)
    try:
        rc = T.main()
    except Exception as e:                       # noqa: BLE001
        rc = 3
        print("❌ 签到过程异常：%s" % e)
    finally:
        sys.stdout = real

    text = buf.getvalue()
    ok_n = claimed_count(text)
    print("\n🔎 [判定输入] 汇总行里「成功」数 = %d" % ok_n)

    after = snapshot("签到后")

    # 真有领取却看不到余额增长时，给服务端几次记账时间
    attempt = 0
    while ok_n > 0 and not moved(before, after) and attempt < REREAD_TIMES:
        attempt += 1
        print("   ⏳ 本次有真实领取但余额未变，%d 秒后重读（第 %d/%d 次）…"
              % (REREAD_WAIT, attempt, REREAD_TIMES))
        time.sleep(REREAD_WAIT)
        after = snapshot("签到后重读 %d" % attempt)

    rows = []
    for key, b in before.items():
        a = after.get(key) or {}
        br, ar = b.get("remain"), a.get("remain")
        rows.append({
            "key": key,
            "name": a.get("name") or b.get("name") or key,
            "before": br, "after": ar,
            "total_before": b.get("total"), "total_after": a.get("total"),
            "delta": None if (br is None or ar is None) else round(ar - br, 2),
            "error": a.get("error") or b.get("error"),
        })

    try:
        with open(DELTA_FILE, "w", encoding="utf-8") as fh:
            json.dump({"claimed": ok_n, "rows": rows}, fh, ensure_ascii=False, indent=2)
        print("🧾 [余额] 对比已写入 %s" % os.path.basename(DELTA_FILE))
    except OSError as e:                         # noqa: BLE001
        print("⚠️ [余额] 写入失败: %s" % e)

    print("\n===== 余额对比（签到前 → 签到后）=====")
    for r in rows:
        if r["before"] is None or r["after"] is None:
            print("  %s：无法对比（%s）" % (r["name"], r.get("error") or "无数据"))
        else:
            sign = "+" if (r["delta"] or 0) > 0 else ""
            print("  %s：%s → %s（%s%s）"
                  % (r["name"], T._fmt(r["before"]), T._fmt(r["after"]),
                     sign, T._fmt(r["delta"])))
    return rc


if __name__ == "__main__":
    sys.exit(main())
