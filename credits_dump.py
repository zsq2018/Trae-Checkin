#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""手动查看积分明细：每笔权益包的**数量 + 到期时间**（只读，不签到、不改状态）。

为什么单独写一个：
  每日通知只报「总额 / 已用 / 剩余」汇总数，看不出**每笔积分什么时候过期**。
  官方规则：每日签到积分有效期 31 个自然日；每月登录赠送的 500 分是「当前自然月」。
  所以"剩余 4600"里往往有几批不同到期日的积分，快过期的那批要优先花掉。

用法：GitHub Actions 里手动触发 `积分明细（手动）` 工作流。
"""

import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import trae_core as T  # noqa: E402

BJ = 8 * 3600


def bj(ts):
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return "-"
    if ts <= 0:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts + BJ))


def pack_amount(p):
    """尽力从权益包里找出「数量/剩余」字段。"""
    for k in ("amount", "total_amount", "total", "quantity", "quota",
              "remain_amount", "remaining_amount", "balance", "num"):
        v = p.get(k)
        if isinstance(v, (int, float)):
            return k, v
    bi = p.get("entitlement_base_info")
    if isinstance(bi, dict):
        for k in ("amount", "total_amount", "total", "quantity"):
            v = bi.get(k)
            if isinstance(v, (int, float)):
                return "entitlement_base_info." + k, v
    return None, None


def describe(p, root):
    """把包里与「这是什么积分、有多少、何时到期」相关的键挑出来。"""
    out = {}
    for k in ("id", "name", "title", "type", "entitlement_type", "status",
              "amount", "total_amount", "total", "quantity", "quota",
              "used_amount", "consumed_amount", "remain_amount",
              "remaining_amount", "balance", "num",
              "expire_time", "start_time", "create_time", "yearly_expire_time"):
        if k in p:
            out[k] = p[k]
    bi = p.get("entitlement_base_info")
    if isinstance(bi, dict):
        for k in ("id", "name", "title", "type", "amount", "total_amount",
                  "start_time", "end_time"):
            if k in bi:
                out["base." + k] = bi[k]
    return out


def main():
    T.setup_log()
    accounts = T.load_accounts()
    print("账号数量: %d" % len(accounts))
    for acc in accounts:
        name = acc.get("name") or acc.get("uid") or "?"
        dev, src = T.resolve_device_id(acc)
        print()
        print("=" * 70)
        print("账号: %s   设备号: %s (%s)" % (name, dev, src))
        ok, msg = T.ensure_token(acc)
        if not ok:
            print("  ❌ 凭证不可用: %s" % msg)
            continue

        headers = T.ug_headers(acc["accessToken"], dev, acc.get("region"))
        status, root, raw = T.http_post(
            T.EP_ENTITLE, headers, '{"require_usage": true, "full_data": true}', 20)
        print("  权益接口 HTTP %s" % status)
        if not isinstance(root, (dict, list)):
            print("  原始响应: %s" % str(raw)[:300])
            continue

        data = root.get("data") if isinstance(root, dict) and isinstance(root.get("data"), dict) else root
        us = T.pick(data, "usage_summary", "usageSummary")
        if isinstance(us, dict):
            print("  用量汇总: %s" % json.dumps(us, ensure_ascii=False))

        packs = T.pick(data, "user_entitlement_pack_list", "userEntitlementPackList") or []
        if not isinstance(packs, list):
            packs = []
        print("  权益包数量: %d" % len(packs))
        print()
        now = time.time()
        for i, p in enumerate(packs):
            if not isinstance(p, dict):
                continue
            exp = p.get("expire_time") or (p.get("entitlement_base_info") or {}).get("end_time")
            left = ""
            try:
                d = (int(exp) - now) / 86400.0
                left = "  剩 %.1f 天" % d
            except (TypeError, ValueError):
                pass
            print("  [%d] 到期 %s%s" % (i, bj(exp), left))

        # 权益包里没有"数量"字段，说明分量挂在别的子结构里 —— 扫全响应找数量类字段
        AMT = ("amount", "total", "num", "count", "quota", "balance", "credit",
               "point", "score", "volume", "quantity", "left", "remain", "used", "consume")
        print()
        print("  —— 响应里所有「数量类」字段（路径 = 值）——")
        n = 0
        for path, val in walk(root):
            key = path.rsplit(".", 1)[-1].lower()
            if any(a in key for a in AMT) and not isinstance(val, (dict, list)):
                print("    %s = %s" % (path, val))
                n += 1
                if n >= 80:
                    break
        if n == 0:
            print("    （未找到；完整结构截断 5000 字符如下）")
            print(json.dumps(root, ensure_ascii=False, indent=1)[:5000])
    print()
    print("完成（只读，未做任何修改）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
