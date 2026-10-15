#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""手动查看积分明细（含到期时间）—— 只读，不签到、不改任何状态。

为什么单独写一个：
  每日通知里只报「总额 / 已用 / 剩余」这几个汇总数，看不出**每笔积分什么时候过期**。
  官方规则说签到积分有效期是 31 个自然日，所以"剩余 4600"里可能有一部分快到期了。
  这个脚本把权益接口的原始结构（含到期字段）打出来，用于人工核对。

用法：在 GitHub Actions 里手动触发 `积分明细（手动）` 工作流。
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import trae_core as T  # noqa: E402

KEYS = ("expire", "expiry", "end_time", "endtime", "deadline", "valid", "effective", "due")


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
        dev, src = T.resolve_device_id(acc)
        print()
        print("=" * 68)
        print("账号: %s   设备号: %s (%s)" % (name, dev, src))
        ok, msg = T.ensure_token(acc)
        if not ok:
            print("  ❌ 凭证不可用: %s" % msg)
            continue

        root = None
        for body in ('{"require_usage": true, "full_data": true}', "{}"):
            headers = T.ug_headers(acc["accessToken"], dev, acc.get("region"))
            status, d, raw = T.http_post(T.EP_ENTITLE, headers, body, 20)
            print("  权益接口 HTTP %s  body=%s" % (status, body))
            if isinstance(d, (dict, list)):
                root = d
                break
            print("   原始响应: %s" % str(raw)[:300])
        if root is None:
            continue

        data = root.get("data") if isinstance(root, dict) and isinstance(root.get("data"), dict) else root
        us = T.pick(data, "usage_summary", "usageSummary")
        if isinstance(us, dict):
            print("  用量汇总: %s" % json.dumps(us, ensure_ascii=False))

        hits = [(p, v) for p, v in walk(root)
                if any(k in p.lower() for k in KEYS)]
        if hits:
            print("  —— 含到期 / 有效期字样的字段 ——")
            for p, v in hits[:60]:
                print("    %s = %s" % (p, v))
        else:
            print("  未发现到期字段；完整结构（截断 4000 字符）:")
            print(json.dumps(root, ensure_ascii=False, indent=1)[:4000])
    print()
    print("完成（只读，未做任何修改）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
