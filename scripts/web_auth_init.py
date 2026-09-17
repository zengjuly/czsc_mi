#!/usr/bin/env python3
"""web_auth_init.py — 初始化/修改 Web 登录凭据（W42 v0.10.15）。

用法:
  python scripts/web_auth_init.py                 # 交互（getpass 两次确认）
  python scripts/web_auth_init.py --user zeng
  WEB_AUTH_PWD='***' python scripts/web_auth_init.py --password-env  # 非交互

写入 ~/.config/czsc_mi/web_auth.json（chmod 600，不进 git；文件存在即启用登录门）。
删除该文件或使用 MYSTERY_WEB_AUTH=0 可关闭鉴权。
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mystery.apps.web.auth import AUTH_FILE, hash_password  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--user", default=os.environ.get("USER", "admin"))
    ap.add_argument("--password-env", action="store_true",
                    help="从环境变量 WEB_AUTH_PWD 读口令（非交互）")
    args = ap.parse_args()

    if args.password_env:
        pwd = os.environ.get("WEB_AUTH_PWD", "")
        if not pwd:
            print("WEB_AUTH_PWD 为空，中止", file=sys.stderr)
            return 1
    else:
        pwd = getpass.getpass("设置 Web 登录密码: ")
        if pwd != getpass.getpass("再输一次确认: "):
            print("两次输入不一致，中止", file=sys.stderr)
            return 1
    if len(pwd) < 8:
        print("密码少于 8 位，拒绝（公网暴露面不嫌长）", file=sys.stderr)
        return 1

    doc = {"user": args.user, **hash_password(pwd)}
    AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    AUTH_FILE.write_text(json.dumps(doc, ensure_ascii=False, indent=1),
                         encoding="utf-8")
    os.chmod(AUTH_FILE, stat.S_IRUSR | stat.S_IWUSR)  # 600
    print(f"已写入 {AUTH_FILE}（user={args.user}, alg=pbkdf2_sha256, "
          f"iterations={doc['iterations']}）；重启 czsc-mi-web 后生效")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
