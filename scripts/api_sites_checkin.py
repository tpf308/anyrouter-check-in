#!/usr/bin/env python3
"""Daily check-in for api.ranmeng.icu and aiapi1.cc.cd."""

from __future__ import annotations

import http.cookiejar
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
ACCOUNTS_FILE = HERE / "api-sites-accounts.json"
LOG_FILE = HERE / "api-sites-checkin.log"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

RANMENG_BASE_URL = os.getenv("RANMENG_BASE_URL", "https://api.ranmeng.icu").rstrip("/")
RANMENG_API_BASE = f"{RANMENG_BASE_URL}/api/v1"
AIAPI1_BASE_URL = os.getenv("AIAPI1_BASE_URL", "https://aiapi1.cc.cd").rstrip("/")


def log(handle: Any, message: str) -> None:
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  {message}"
    print(line)
    if handle:
        handle.write(line + "\n")
        handle.flush()


def load_accounts() -> dict[str, list[dict[str, Any]]]:
    raw = os.getenv("API_SITES_ACCOUNTS") or os.getenv("CHECKIN_ACCOUNTS")
    if raw:
        data = json.loads(raw)
    elif ACCOUNTS_FILE.exists():
        data = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8-sig"))
    else:
        raise RuntimeError("No accounts configured. Set API_SITES_ACCOUNTS or create api-sites-accounts.json.")

    if not isinstance(data, dict):
        raise RuntimeError("API_SITES_ACCOUNTS must be a JSON object.")
    result: dict[str, list[dict[str, Any]]] = {}
    for site in ("ranmeng",):
        items = data.get(site, []) or []
        if not isinstance(items, list):
            raise RuntimeError(f"{site} must be a JSON array.")
        result[site] = items
    if not result["ranmeng"]:
        raise RuntimeError("No accounts found under ranmeng.")
    return result


def unwrap_sub2api(payload: Any) -> Any:
    if isinstance(payload, dict) and "code" in payload:
        if payload.get("code") == 0:
            return payload.get("data")
        raise RuntimeError(payload.get("message") or json.dumps(payload, ensure_ascii=False))
    return payload


def read_json_response(req: urllib.request.Request, opener: Any | None = None) -> Any:
    try:
        if opener is None:
            resp_ctx = urllib.request.urlopen(req, timeout=30)
        else:
            resp_ctx = opener.open(req, timeout=30)
        with resp_ctx as resp:
            text = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(text)
            message = payload.get("message") or payload.get("error") or payload.get("detail") or text[:300]
        except json.JSONDecodeError:
            message = text[:300]
        raise RuntimeError(f"HTTP {exc.code}: {message}") from exc

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Non-JSON response: {text[:300]!r}") from exc


class RanmengClient:
    def __init__(self) -> None:
        self.access_token: str | None = None

    def request(self, method: str, path: str, body: dict[str, Any] | None = None, auth: bool = True) -> Any:
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Content-Type": "application/json",
            "Origin": RANMENG_BASE_URL,
            "Referer": f"{RANMENG_BASE_URL}/checkin",
            "User-Agent": USER_AGENT,
        }
        if auth and self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        req = urllib.request.Request(f"{RANMENG_API_BASE}{path}", data=data, headers=headers, method=method.upper())
        return unwrap_sub2api(read_json_response(req))

    def login(self, email: str, password: str) -> None:
        data = self.request("POST", "/auth/login", {"email": email, "password": password}, auth=False)
        if not isinstance(data, dict):
            raise RuntimeError("Login returned an invalid response.")
        if data.get("requires_2fa"):
            raise RuntimeError("Login requires 2FA; unattended check-in cannot continue.")
        access_token = data.get("access_token")
        if not access_token:
            raise RuntimeError("Login succeeded but no access_token was returned.")
        self.access_token = str(access_token)

    def daily_status(self) -> dict[str, Any]:
        data = self.request("GET", "/user/daily-checkin")
        return data if isinstance(data, dict) else {}

    def start_challenge(self) -> dict[str, Any]:
        data = self.request("POST", "/user/daily-checkin/challenge", {})
        return data if isinstance(data, dict) else {}

    def claim(self, challenge_token: str) -> dict[str, Any]:
        data = self.request("POST", "/user/daily-checkin", {"challenge_token": challenge_token})
        return data if isinstance(data, dict) else {}


class AiApi1Client:
    def __init__(self) -> None:
        self.user_id = "-1"
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Cache-Control": "no-store",
            "Content-Type": "application/json",
            "New-API-User": str(self.user_id),
            "Origin": AIAPI1_BASE_URL,
            "Referer": f"{AIAPI1_BASE_URL}/console/personal",
            "User-Agent": USER_AGENT,
        }
        req = urllib.request.Request(f"{AIAPI1_BASE_URL}{path}", data=data, headers=headers, method=method.upper())
        payload = read_json_response(req, opener=self.opener)
        if not isinstance(payload, dict):
            raise RuntimeError("Invalid JSON response.")
        return payload

    def login(self, username: str, password: str) -> None:
        payload = self.request("POST", "/api/user/login", {"username": username, "password": password})
        if not payload.get("success"):
            raise RuntimeError(payload.get("message") or "Login failed.")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise RuntimeError("Login returned no user data.")
        if data.get("require_2fa"):
            raise RuntimeError("Login requires 2FA; unattended check-in cannot continue.")
        user_id = data.get("id")
        if user_id is None:
            raise RuntimeError("Login succeeded but no user id was returned.")
        self.user_id = str(user_id)

    def checkin_status(self) -> dict[str, Any]:
        month = datetime.now().strftime("%Y-%m")
        payload = self.request("GET", f"/api/user/checkin?{urllib.parse.urlencode({'month': month})}")
        if not payload.get("success"):
            raise RuntimeError(payload.get("message") or "Failed to get check-in status.")
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    def checkin(self) -> dict[str, Any]:
        payload = self.request("POST", "/api/user/checkin", {})
        if not payload.get("success"):
            message = payload.get("message") or "Check-in failed."
            if "Turnstile" in str(message):
                raise RuntimeError("Check-in requires Turnstile verification; unattended check-in cannot continue.")
            raise RuntimeError(message)
        data = payload.get("data")
        return data if isinstance(data, dict) else {}


def check_ranmeng(account: dict[str, Any]) -> tuple[bool, str]:
    email = account.get("email")
    password = account.get("password")
    if not email or not password:
        return False, "missing email/password"
    client = RanmengClient()
    client.login(str(email), str(password))
    status = client.daily_status()
    if status.get("today_checked_in") or not status.get("can_checkin"):
        return True, f"already checked in, reward={status.get('today_reward')}, balance={status.get('current_balance')}"
    challenge = client.start_challenge()
    challenge_token = challenge.get("token")
    wait_seconds = int(challenge.get("wait_seconds") or 0)
    if not challenge_token:
        status = client.daily_status()
        return True, f"challenge not required, checked={status.get('today_checked_in')}"
    if wait_seconds > 0:
        time.sleep(min(wait_seconds + 1, 90))
    claimed = client.claim(str(challenge_token))
    return True, f"checked in, reward={claimed.get('today_reward') or claimed.get('reward')}, balance={claimed.get('current_balance')}"


def check_aiapi1(account: dict[str, Any]) -> tuple[bool, str]:
    username = account.get("username")
    password = account.get("password")
    if not username or not password:
        return False, "missing username/password"
    client = AiApi1Client()
    client.login(str(username), str(password))
    status = client.checkin_status()
    stats = status.get("stats") if isinstance(status.get("stats"), dict) else {}
    if stats.get("checked_in_today"):
        count = stats.get("checkin_count") or stats.get("total_checkins")
        return True, f"already checked in, total_quota={stats.get('total_quota')}, count={count}"
    claimed = client.checkin()
    return True, f"checked in, quota_awarded={claimed.get('quota_awarded')}"


def run_site(handle: Any, site: str, accounts: list[dict[str, Any]]) -> tuple[int, int]:
    if not accounts:
        return 0, 0
    checker = check_ranmeng if site == "ranmeng" else check_aiapi1
    ok = 0
    log(handle, f"===== {site}: {len(accounts)} account(s) =====")
    for idx, account in enumerate(accounts):
        name = account.get("name") or account.get("email") or account.get("username") or f"{site}-{idx + 1}"
        try:
            success, detail = checker(account)
        except Exception as exc:
            success, detail = False, f"{type(exc).__name__}: {exc}"
        if success:
            ok += 1
            log(handle, f"  OK   {name}: {detail}")
        else:
            log(handle, f"  FAIL {name}: {detail}")
    return ok, len(accounts)


def main() -> int:
    try:
        accounts = load_accounts()
    except Exception as exc:
        print(f"FATAL: {exc}")
        return 1
    total_ok = 0
    total_count = 0
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        log(handle, "===== API sites check-in start =====")
        for site in ("ranmeng",):
            ok, count = run_site(handle, site, accounts[site])
            total_ok += ok
            total_count += count
        log(handle, f"===== Done: {total_ok}/{total_count} succeeded =====\n")
    return 0 if total_ok == total_count else 2


if __name__ == "__main__":
    sys.exit(main())
