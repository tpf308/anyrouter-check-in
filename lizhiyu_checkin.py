#!/usr/bin/env python3
"""
lizhiyu.appleinc.cn 每日自动签到（独立脚本，跑在 GitHub Actions）。

这是一个自定义中转站（非标准 NewAPI），签到是独立接口，流程为：
  1. POST /v1/user/login-pwd  {email, password}            -> {token, nickname, ...}
  2. POST /v1/user/profile    {} + Bearer token            -> {balance, ...}（取余额）
  3. POST /v1/user/signin     {} + Bearer token            -> {reward}（当天首次）
                                                              或 400 already_signed_in（已签到=成功）

账号来自环境变量 LIZHIYU_ACCOUNTS（JSON 数组）：
  [{"email": "...", "password": "...", "name": "可选别名"}, ...]
失败时通过 SMTP 发邮件（复用 EMAIL_USER / EMAIL_PASS / EMAIL_TO 环境变量），全部成功则静默。
退出码：全部成功 0，否则 2。
"""

import json
import os
import smtplib
import sys
import time
from datetime import datetime
from email.message import EmailMessage

import httpx

BASE = 'https://lizhiyu.appleinc.cn'
USER_API = BASE + '/v1/user'

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/plain, */*',
    'Content-Type': 'application/json; charset=utf-8',
    'Origin': BASE,
    'Referer': BASE + '/',
}


def log(msg: str) -> None:
    print(f'{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}  {msg}', flush=True)


def _err_code(r: httpx.Response) -> str:
    """从 {"error":{"code":...,"message":...}} 里取 code/message。"""
    try:
        e = r.json().get('error', {})
        return e.get('code') or e.get('message') or r.text[:80]
    except Exception:
        return f'status {r.status_code}: {r.text[:80]!r}'


def check_in_one(client: httpx.Client, email: str, password: str) -> tuple[bool, str]:
    """登录 -> 取余额 -> 签到。返回 (是否成功, 详情)。"""
    # 1) 登录拿 token
    r = client.post(f'{USER_API}/login-pwd', json={'email': email, 'password': password})
    if 'json' not in r.headers.get('content-type', ''):
        return False, f'login non-JSON (status {r.status_code}): {r.text[:80]!r}'
    if r.status_code != 200:
        return False, f'login failed: {_err_code(r)}'
    token = r.json().get('token')
    if not token:
        return False, 'login ok but no token in response'

    auth = {**HEADERS, 'Authorization': 'Bearer ' + token}

    # 2) 取余额（失败不致命，仅影响显示）
    bal = '?'
    for attempt in range(3):
        p = client.post(f'{USER_API}/profile', json={}, headers=auth)
        if p.status_code == 200 and 'json' in p.headers.get('content-type', ''):
            bal = f'${p.json().get("balance", "?")}'
            break
        if attempt < 2:
            time.sleep(1.5)

    # 3) 签到
    s = client.post(f'{USER_API}/signin', json={}, headers=auth)
    if s.status_code == 200:
        try:
            reward = s.json().get('reward')
        except Exception:
            reward = None
        tip = f'signed in now (reward ¥{reward})' if reward is not None else 'signed in now'
        return True, f'{tip}, balance {bal}'

    code = _err_code(s)
    if code == 'already_signed_in' or 'already' in str(code).lower() or '已签到' in str(code):
        return True, f'already signed in today, balance {bal}'
    return False, f'signin failed: {code}'


def notify_email(subject: str, body: str) -> tuple[bool, str]:
    """SMTP 失败告警。复用 EMAIL_USER/EMAIL_PASS/EMAIL_TO。465->SSL，其余->STARTTLS。永不抛异常。"""
    user = (os.getenv('EMAIL_USER') or '').strip()
    password = (os.getenv('EMAIL_PASS') or '').strip()
    to = (os.getenv('EMAIL_TO') or '').strip() or user
    server = (os.getenv('CUSTOM_SMTP_SERVER') or '').strip() or (f'smtp.{user.split("@")[1]}' if '@' in user else '')
    port = int(os.getenv('EMAIL_PORT') or 465)
    if not (user and password and to and server):
        return False, 'email config incomplete'

    msg = EmailMessage()
    msg['From'] = f'Lizhiyu Checkin <{user}>'
    msg['To'] = to
    msg['Subject'] = subject
    msg.set_content(body)
    try:
        if port == 465:
            with smtplib.SMTP_SSL(server, port, timeout=30) as srv:
                srv.login(user, password)
                srv.send_message(msg)
        else:
            with smtplib.SMTP(server, port, timeout=30) as srv:
                srv.ehlo()
                srv.starttls()
                srv.ehlo()
                srv.login(user, password)
                srv.send_message(msg)
    except Exception as e:
        return False, f'{type(e).__name__}: {e}'
    return True, f'sent to {to}'


def main() -> int:
    raw = os.getenv('LIZHIYU_ACCOUNTS', '').strip()
    if not raw:
        log('FATAL: 环境变量 LIZHIYU_ACCOUNTS 未设置')
        return 1
    try:
        accounts = json.loads(raw)
        assert isinstance(accounts, list) and accounts
    except Exception as e:
        log(f'FATAL: LIZHIYU_ACCOUNTS 不是合法的非空 JSON 数组: {e}')
        return 1

    results = []
    ok = 0
    log(f'===== lizhiyu check-in: {len(accounts)} account(s) =====')
    for acc in accounts:
        email = acc.get('email', '')
        name = acc.get('name') or email
        try:
            with httpx.Client(http2=True, timeout=30, headers=HEADERS, follow_redirects=True) as c:
                success, detail = check_in_one(c, email, acc.get('password', ''))
        except Exception as e:
            success, detail = False, f'{type(e).__name__}: {e}'
        results.append((name, success, detail))
        if success:
            ok += 1
            log(f'  OK   {name}: {detail}')
        else:
            log(f'  FAIL {name}: {detail}')
    log(f'===== Done: {ok}/{len(accounts)} succeeded =====')

    # 仅失败时发邮件，全部成功保持静默。
    if ok < len(accounts) and os.getenv('EMAIL_USER') and os.getenv('EMAIL_PASS'):
        fail = len(accounts) - ok
        subject = f'⚠️ Lizhiyu 签到失败 {fail}/{len(accounts)}'
        lines = [f'{"✅" if s else "❌"}  {n}: {d}' for n, s, d in results]
        lines += ['', f'时间：{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}', f'站点：{BASE}']
        pok, pdetail = notify_email(subject, '\n'.join(lines))
        log(f'  notify: {"OK" if pok else "FAIL"} ({pdetail})')

    return 0 if ok == len(accounts) else 2


if __name__ == '__main__':
    sys.exit(main())
