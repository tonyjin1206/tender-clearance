#!/usr/bin/env python3
"""SRM 实时查询自测（P5 联调用）。

用法（在已接入内网的终端中，凭据只进本进程内存，绝不写入任何文件）：

    export SRM_USER='<账号或appKey>'
    export SRM_PASSWORD='<密码或appSecret>'
    python scripts/srm_selftest.py '<企业名称或统一社会信用代码>'

输出：鉴权结果、查询状态与命中字段（不含凭据）。
"""

from __future__ import annotations

import getpass
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tc.srm import SrmClient, SrmCredentials, load_srm_config
from tc.sources import QuerySubject


def main() -> int:
    keyword = sys.argv[1] if len(sys.argv) > 1 else ""
    if not keyword:
        print("用法：srm_selftest.py '<企业名称或统一社会信用代码>'")
        return 2
    user = os.environ.get("SRM_USER", "")
    password = os.environ.get("SRM_PASSWORD", "")
    try:
        if not user:
            user = input("SRM 账号（appKey/用户名）: ")
        if not password:
            password = getpass.getpass("SRM 密码（appSecret/密码）: ")
    except EOFError:
        print("[提示] 未提供凭据（设置 SRM_USER / SRM_PASSWORD 环境变量后重试）")
        return 2

    cfg = load_srm_config()
    auth_mode = (cfg.get("auth") or {}).get("mode", "none")
    print(f"配置：base_url={cfg.get('base_url')}  auth.mode={auth_mode}")
    if auth_mode == "none":
        print("[错误] rules/srm-api.yaml 的 auth.mode 仍为 none，未配置 API 规范")
        return 2

    import re

    uscc_m = re.search(r"[0-9A-HJ-NPQRTUWXY]{18}", keyword, re.I)
    subject = QuerySubject(
        supplier_id=None,
        name=None if uscc_m else keyword,
        uscc=uscc_m.group(0).upper() if uscc_m else None,
    )
    print(f"主体键：uscc={subject.uscc}  name={subject.name}")

    client = SrmClient(cfg, SrmCredentials(user, password))
    result = client.query(subject, datetime.now(timezone.utc))
    print(f"状态：{result.status}")
    if result.detail:
        print(f"说明：{result.detail}")
    if result.records:
        print(f"命中 {result.parsed_count} 条：")
        for rec in result.records:
            print(f"  - 主体确认: {rec.subject_confirmation}")
            for k, v in rec.fields.items():
                print(f"      {k}: {v}")
    return 0 if result.status in ("match", "no_match_verified") else 1


if __name__ == "__main__":
    raise SystemExit(main())
