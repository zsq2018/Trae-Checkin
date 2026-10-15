#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""查看积分明细：每笔权益包的**数量 + 到期时间**（只读，不签到、不改任何状态）。

为什么单独写一个：
  每日通知只报「总额 / 已用 / 剩余」汇总数，看不出**每笔积分什么时候过期**。
  官方规则（https://docs.trae.cn/ide_plans-and-billing）：
    · 每日签到积分    有效期 31 个自然日（免费用户 100/天，会员 200/天）
    · 每月登录赠送 500 有效期「当前自然月」，月底清零
    · 新用户注册福利  31 个自然日
    · 消耗顺序：优先消耗剩余有效期最短的积分
  所以"剩余 N 分"里往往有几批不同到期日的积分，快过期的那批要优先花掉。

用法：GitHub Actions 页面手动触发「积分明细（手动）」工作流。字段来自接口的
     user_entitlement_pack_list[*].entitlement_base_info.quota.credits_limit。
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


def days_left(ts):
    try:
        return (int(ts) - time.time()) / 86400.0
    except (TypeError, ValueError):
        return None


def pack_info(p):
    """返回 (分数, 已用, 到期时间戳)。分数取 quota.credits_limit。"""
    bi = p.get("entitlement_base_info") or {}
    limit = (bi.get("quota") or {}).get("credits_limit")
    if limit is None:
        pe = bi.get("product_extra") or {}
        for key in ("package_extra", "subscription_extra"):
            q = ((pe.get(key) or {}).get("quota") or {})
            if q.get("credits_limit") is not None:
                limit = q["credits_limit"]
                break
    used = ((p.get("usage") or {}).get("credits_amount"))
    exp = p.get("expire_time") or bi.get("end_time")
    return limit, used, exp


def walk(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from walk(v, path + "." + str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:50]):
            yield from walk(v, path + "[%d]" % i)
    else:
        yield path, obj


def main():
    T.setup_log()
    accounts = T.load_accounts()
    print("账号数量: %d" % len(accounts))
    for acc in accounts:
        name = acc.get("name") or acc.get("uid") or "?"
        print()
        print("=" * 70)
        print("账号: %s" % name)
        try:
            dev, src = T.resolve_device_id(acc)
            ok, msg = T.ensure_token(acc)
            if not ok:
                print("  ❌ 凭证不可用: %s" % msg)
                continue
            headers = T.ug_headers(acc["accessToken"], dev, acc.get("region"))
            status, root, raw = T.http_post(
                T.EP_ENTITLE, headers, '{"require_usage": true, "full_data": true}', 20)
            print("  设备号: %s (%s)   权益接口 HTTP %s" % (dev, src, status))
            if not isinstance(root, (dict, list)):
                print("  原始响应: %s" % str(raw)[:300])
                continue

            data = root.get("data") if isinstance(root, dict) and isinstance(root.get("data"), dict) else root
            us = T.pick(data, "usage_summary", "usageSummary")
            if isinstance(us, dict):
                print("  汇总: 总额 %s · 已用 %s · 剩余 %s"
                      % (us.get("total_amount"), us.get("consumed_amount"),
                         round(float(us.get("total_amount") or 0) - float(us.get("consumed_amount") or 0), 2)))

            packs = T.pick(data, "user_entitlement_pack_list", "userEntitlementPackList") or []
            if not isinstance(packs, list):
                packs = []
            print("  权益包 %d 个：" % len(packs))
            total = 0.0
            for i, p in enumerate(packs):
                if not isinstance(p, dict):
                    continue
                limit, used, exp = pack_info(p)
                d = days_left(exp)
                d_txt = ("剩 %.1f 天" % d) if d is not None else ""
                if isinstance(limit, (int, float)):
                    total += float(limit)
                print("    [%d] %-6s 分  已用 %-8s 到期 %s  %s"
                      % (i,
                         ("%g" % limit) if isinstance(limit, (int, float)) else "(非积分/未知)",
                         ("%g" % used) if isinstance(used, (int, float)) else "-",
                         bj(exp), d_txt))
            print("  权益包分数合计: %g（应与上面「总额」一致）" % total)

            soon = []
            for p in packs:
                if not isinstance(p, dict):
                    continue
                limit, _used, exp = pack_info(p)
                d = days_left(exp)
                if isinstance(limit, (int, float)) and d is not None and 0 <= d <= 20:
                    soon.append((exp, limit))
            if soon:
                print("  ⚠️ 20 天内到期：")
                for exp, limit in sorted(soon):
                    print("      %s 到期 %g 分" % (bj(exp), limit))
        except Exception as e:                   # noqa: BLE001
            print("  ❌ 处理出错: %s: %s" % (type(e).__name__, e))
    print()
    print("完成（只读，未做任何修改）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
