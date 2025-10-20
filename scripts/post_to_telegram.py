#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Post next coupon to Telegram channel.
Designed to be run by GitHub Actions every 2 hours.
Saves published coupon ids to state.json and commits it back to repo.
"""

import os
import sys
import json
import subprocess
from datetime import datetime, timezone
from time import sleep

import requests
from dateutil import parser as date_parser

# Config (can override via env)
API_URL = os.getenv("COUPONS_API_URL", "https://receivecoupons.com/api/my_api.php")
STATE_FILE = os.getenv("STATE_FILE", "state.json")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")  # e.g. @receivecoupons
GIT_COMMIT_NAME = "github-actions[bot]"
GIT_COMMIT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"

REQUESTS_TIMEOUT = 15  # seconds
TELEGRAM_CAPTION_MAX = 1000  # safe margin (telegram limit ~1024)

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    print("ERROR: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be provided in env.", file=sys.stderr)
    sys.exit(2)

def load_state(path):
    if not os.path.exists(path):
        return {"published_ids": [], "last_run": None}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_state(path, state):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def git_commit_and_push(path, message="Update state.json"):
    # Assumes actions/checkout with persist-credentials and GITHUB_TOKEN available
    try:
        subprocess.check_call(["git", "config", "user.name", GIT_COMMIT_NAME])
        subprocess.check_call(["git", "config", "user.email", GIT_COMMIT_EMAIL])
        subprocess.check_call(["git", "add", path])
        subprocess.check_call(["git", "commit", "-m", message])
        subprocess.check_call(["git", "push"])
        print("State committed and pushed.")
    except subprocess.CalledProcessError as e:
        print("Git commit/push failed:", e, file=sys.stderr)

def fetch_coupons():
    try:
        r = requests.get(API_URL, timeout=REQUESTS_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        # Expect either dict with key 'data' or a list. Normalize:
        if isinstance(data, dict):
            # common patterns: { "data": [...]} or list inside
            if "data" in data and isinstance(data["data"], list):
                return data["data"]
            # if the API returns object with numeric keys
            # fallback: try to extract list members
            for v in data.values():
                if isinstance(v, list):
                    return v
            return []
        elif isinstance(data, list):
            return data
        else:
            return []
    except Exception as e:
        print("Failed to fetch coupons:", e, file=sys.stderr)
        return []

def is_valid_coupon(coupon):
    try:
        if int(coupon.get("is_visible", 0)) != 1:
            return False
        expires = coupon.get("expires_at")
        if not expires:
            return True  # no expiry -> treat as valid
        dt = date_parser.parse(expires)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt > datetime.now(timezone.utc)
    except Exception as e:
        print("Error checking coupon expiry:", e, file=sys.stderr)
        return False

def make_caption(c):
    # Fields: title, discount_text, code, countries, note, expires_at, purchase_link
    parts = []
    
    # العنوان
    title = c.get("title") or ""
    if title:
        parts.append(f"🎉 {title}")
        parts.append("")
    
    # نص الخصم
    discount_text = c.get("discount_text") or ""
    if discount_text:
        parts.append(f"🔥 {discount_text}")
        parts.append("")
    
    # الكوبون
    code = c.get("code") or ""
    if code:
        parts.append(f"🎁 الكوبون : {code}")
        parts.append("")
    
    # الدول
    countries = c.get("countries") or ""
    if countries:
        parts.append(f"🌍 صالح لـ : {countries}")
        parts.append("")
    
    # الملاحظة
    note = c.get("note") or ""
    if note:
        parts.append(f"📌 ملاحظة : {note}")
        parts.append("")
    
    # تاريخ الانتهاء
    expires = c.get("expires_at") or ""
    if expires:
        parts.append(f"⏳ ينتهي في : {expires}")
        parts.append("")
    
    # رابط الشراء
    link = c.get("purchase_link") or ""
    if link:
        parts.append(f"🛒 رابط الشراء : {link}")
        parts.append("")
    
    # رابط الموقع
    parts.append("💎 لمزيد من الكوبونات زوروا موقعنا :")
    parts.append("")
    parts.append(" https://receivecoupons.com/")
    
    caption = "\n".join(parts).strip()

    # truncate if too long
    if len(caption) > TELEGRAM_CAPTION_MAX:
        caption = caption[: TELEGRAM_CAPTION_MAX - 3] + "..."
    return caption

def post_to_telegram_photo(photo_url, caption_html):
    send_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "photo": photo_url,
        "caption": caption_html,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        r = requests.post(send_url, data=payload, timeout=REQUESTS_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print("Failed to send photo to Telegram:", e, file=sys.stderr)
        return None

def main():
    state = load_state(STATE_FILE)
    published_ids = set(state.get("published_ids", []))

    coupons = fetch_coupons()
    if not coupons:
        print("No coupons fetched. Exiting.")
        sys.exit(0)

    # keep only valid coupons and sort by created_at or id for deterministic order
    valid = [c for c in coupons if is_valid_coupon(c)]
    if not valid:
        print("No valid (visible and not expired) coupons found.")
        # Optionally reset published list if no valid coupons
        sys.exit(0)

    # Sort: prefer created_at desc? We choose created_at asc (oldest first) so that new ones not posted earlier.
    def sort_key(c):
        try:
            return date_parser.parse(c.get("created_at") or c.get("expires_at") or "1970-01-01")
        except:
            return datetime.now(timezone.utc)

    valid_sorted = sorted(valid, key=sort_key)

    # find next unposted coupon
    next_coupon = None
    for c in valid_sorted:
        cid = int(c.get("coupon_id") or c.get("id") or 0)
        if cid not in published_ids:
            next_coupon = c
            break

    # If all coupons have been published, reset published_ids (start over).
    if next_coupon is None:
        print("All coupons already published. Resetting published list and selecting first coupon.")
        published_ids = set()
        state["published_ids"] = []
        # pick first from sorted
        next_coupon = valid_sorted[0]

    if not next_coupon:
        print("No coupon to post. Exiting.")
        sys.exit(0)

    photo_url = next_coupon.get("store", {}).get("logo_url") or next_coupon.get("logo_url") or ""
    caption = make_caption(next_coupon)

    # If there's no photo URL, Telegram allows posting text via sendMessage; we'll prefer sendPhoto when available.
    if photo_url:
        resp = post_to_telegram_photo(photo_url, caption)
    else:
        # fallback to sendMessage
        send_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": caption,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        }
        try:
            r = requests.post(send_url, data=payload, timeout=REQUESTS_TIMEOUT)
            r.raise_for_status()
            resp = r.json()
        except Exception as e:
            print("Failed to send message to Telegram:", e, file=sys.stderr)
            resp = None

    if resp and resp.get("ok"):
        # mark as published
        try:
            cid = int(next_coupon.get("coupon_id") or next_coupon.get("id") or 0)
            published_ids.add(cid)
            state["published_ids"] = sorted(list(published_ids))
            state["last_run"] = datetime.now(timezone.utc).isoformat()
            save_state(STATE_FILE, state)
            print(f"Posted coupon {cid} successfully.")
            # commit state file back to repo
            git_commit_and_push(STATE_FILE, message=f"chore: mark coupon {cid} as published")
        except Exception as e:
            print("Failed updating state after post:", e, file=sys.stderr)
    else:
        print("Telegram API did not return ok. Response:", resp, file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
