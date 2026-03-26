import base64
import json
import os
import re
import urllib.parse
from datetime import datetime, timedelta

import pytz
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from bs4 import BeautifulSoup
from flask import Flask, Response, jsonify
from playwright.sync_api import sync_playwright

app = Flask(__name__)

SOURCE_JS_URL = "https://im-imgs-bucket.oss-accelerate.aliyuncs.com/index.js?t_5"
OUTPUT_FILE = "output/extracted_ids.txt"
ENTRY_FILE = "output/extracted_entries.json"
DEBUG_FILE = "output/debug_last.json"
LAST_RUN_TIME = "尚未执行"
FETCH_WINDOW_HOURS = 1


# ==========================================
# 核心：内置轻量级 XXTEA 解密算法
# ==========================================
def str2long(s, w):
    v = []
    for i in range(0, len(s), 4):
        v0 = s[i]
        v1 = s[i + 1] if i + 1 < len(s) else 0
        v2 = s[i + 2] if i + 2 < len(s) else 0
        v3 = s[i + 3] if i + 3 < len(s) else 0
        v.append(v0 | (v1 << 8) | (v2 << 16) | (v3 << 24))
    if w:
        v.append(len(s))
    return v


def long2str(v, w):
    vl = len(v)
    if vl == 0:
        return b""
    n = (vl - 1) << 2
    if w:
        m = v[-1]
        if (m < n - 3) or (m > n):
            return None
        n = m
    s = bytearray()
    for i in range(vl):
        s.append(v[i] & 0xFF)
        s.append((v[i] >> 8) & 0xFF)
        s.append((v[i] >> 16) & 0xFF)
        s.append((v[i] >> 24) & 0xFF)
    return bytes(s[:n]) if w else bytes(s)


def xxtea_decrypt(data, key):
    if not data:
        return b""
    v = str2long(data, False)
    k = str2long(key, False)
    if len(k) < 4:
        k.extend([0] * (4 - len(k)))
    n = len(v) - 1
    if n < 1:
        return b""

    z = v[n]
    y = v[0]
    delta = 0x9E3779B9
    q = 6 + 52 // (n + 1)
    sum_val = (q * delta) & 0xFFFFFFFF

    while sum_val != 0:
        e = (sum_val >> 2) & 3
        for p in range(n, 0, -1):
            z = v[p - 1]
            mx = (((z >> 5) ^ (y << 2)) + ((y >> 3) ^ (z << 4))) ^ (
                (sum_val ^ y) + (k[(p & 3) ^ e] ^ z)
            )
            y = v[p] = (v[p] - mx) & 0xFFFFFFFF
        z = v[n]
        mx = (((z >> 5) ^ (y << 2)) + ((y >> 3) ^ (z << 4))) ^ (
            (sum_val ^ y) + (k[(0 & 3) ^ e] ^ z)
        )
        y = v[0] = (v[0] - mx) & 0xFFFFFFFF
        sum_val = (sum_val - delta) & 0xFFFFFFFF

    return long2str(v, True)


def parse_match_time(raw_time, tz, now):
    try:
        base = datetime.strptime(raw_time, "%m-%d %H:%M")
    except ValueError:
        return None

    year = now.year
    candidates = [
        tz.localize(base.replace(year=year - 1)),
        tz.localize(base.replace(year=year)),
        tz.localize(base.replace(year=year + 1)),
    ]
    return min(candidates, key=lambda dt: abs(dt - now))


def load_existing_entries():
    if not os.path.exists(ENTRY_FILE):
        return []
    try:
        with open(ENTRY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def parse_entry_datetime(entry, tz):
    match_dt = entry.get("match_datetime")
    if isinstance(match_dt, str):
        try:
            parsed = datetime.fromisoformat(match_dt)
            return parsed if parsed.tzinfo else tz.localize(parsed)
        except ValueError:
            pass

    time_text = entry.get("time")
    if isinstance(time_text, str):
        try:
            parsed = datetime.strptime(time_text, "%m-%d %H:%M")
            now = datetime.now(tz)
            candidates = [
                tz.localize(parsed.replace(year=now.year - 1)),
                tz.localize(parsed.replace(year=now.year)),
                tz.localize(parsed.replace(year=now.year + 1)),
            ]
            return min(candidates, key=lambda dt: abs(dt - now))
        except ValueError:
            return None
    return None


def build_keep_window(now):
    keep_start = (now - timedelta(days=1)).replace(hour=20, minute=0, second=0, microsecond=0)
    keep_end = now.replace(hour=23, minute=59, second=59, microsecond=999999)
    return keep_start, keep_end


def merge_and_filter_entries(existing_entries, new_entries, now, tz):
    keep_start, keep_end = build_keep_window(now)
    merged = {}

    for entry in existing_entries + new_entries:
        raw_id = entry.get("id", "")
        if not raw_id:
            continue
        match_dt = parse_entry_datetime(entry, tz)
        if not match_dt:
            continue
        if not (keep_start <= match_dt <= keep_end):
            continue

        normalized = {
            "league": entry.get("league", ""),
            "time": match_dt.strftime("%m-%d %H:%M"),
            "home": entry.get("home", ""),
            "away": entry.get("away", ""),
            "id": raw_id,
            "match_datetime": match_dt.isoformat(),
        }
        dedupe_key = (
            normalized["league"],
            normalized["time"],
            normalized["home"],
            normalized["away"],
            normalized["id"],
        )
        merged[dedupe_key] = normalized

    return sorted(merged.values(), key=lambda x: x["match_datetime"])


def extract_source_html(js_text):
    pieces = re.findall(r"document\.write\('(.*?)'\);", js_text, flags=re.DOTALL)
    return "".join(piece.replace("\\'", "'") for piece in pieces)


def pick_hd_play_links_from_soup(soup):
    links = []
    for a_tag in soup.select("a[data-play]"):
        label = a_tag.get_text(" ", strip=True)
        data_play = a_tag.get("data-play", "").strip()
        if not data_play.startswith("/play/"):
            continue
        if "高清直播" in label or "蓝光" in label:
            links.append(data_play)
    return links


# ==========================================
# 爬虫任务逻辑
# ==========================================
def scrape_job(debug=False, ignore_time_filter=False):
    global LAST_RUN_TIME
    tz = pytz.timezone("Asia/Shanghai")
    now = datetime.now(tz)
    LAST_RUN_TIME = now.strftime("%Y-%m-%d %H:%M:%S")
    debug_info = {
        "run_time": LAST_RUN_TIME,
        "source_url": SOURCE_JS_URL,
        "fetch_window_hours": FETCH_WINDOW_HOURS,
        "ignore_time_filter": ignore_time_filter,
    }
    print(f"[{LAST_RUN_TIME}] 开始执行抓取任务...")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://www.74001.tv/",
    }

    try:
        js_resp = requests.get(SOURCE_JS_URL, headers=headers, timeout=15)
        js_resp.raise_for_status()
        html = extract_source_html(js_resp.text)
        soup = BeautifulSoup(html, "html.parser")
        debug_info["source_status"] = js_resp.status_code
        debug_info["source_size"] = len(js_resp.text)
        debug_info["parsed_ul_count"] = len(soup.select("ul.item.play"))
    except Exception as e:
        print(f"读取源 JS 失败: {e}")
        debug_info["error"] = f"读取源 JS 失败: {e}"
        if debug:
            return debug_info
        return None

    lower_bound = now - timedelta(hours=FETCH_WINDOW_HOURS)
    upper_bound = now + timedelta(hours=FETCH_WINDOW_HOURS)

    match_links = []
    all_matches = 0
    window_matches = 0
    for ul in soup.select("ul.item.play"):
        all_matches += 1
        league = ul.select_one("li.lab_events span.name")
        match_time_raw = ul.select_one("li.lab_time")
        home = ul.select_one("li.lab_team_home strong.name")
        away = ul.select_one("li.lab_team_away strong.name")

        if not (league and match_time_raw and home and away):
            continue

        match_time = parse_match_time(match_time_raw.get_text(strip=True), tz, now)
        in_window = bool(match_time and (lower_bound <= match_time <= upper_bound))
        if not ignore_time_filter and not in_window:
            continue
        if in_window:
            window_matches += 1

        for a_tag in ul.select("li.lab_channel a.me[href]"):
            href = a_tag.get("href", "").strip()
            if "play.sportsteam368.com" in href:
                match_links.append(
                    {
                        "league": league.get_text(strip=True),
                        "time": match_time.strftime("%m-%d %H:%M") if match_time else match_time_raw.get_text(strip=True),
                        "home": home.get_text(strip=True),
                        "away": away.get_text(strip=True),
                        "href": href,
                        "match_datetime": match_time.isoformat() if match_time else "",
                    }
                )

    debug_info["all_match_count"] = all_matches
    debug_info["window_match_count"] = window_matches
    debug_info["matched_link_count"] = len(match_links)
    debug_info["match_link_samples"] = match_links[:5]

    if not match_links:
        print("时间窗口内未找到可处理的比赛链接。")
        debug_info["error"] = "未匹配到比赛链接（可能是时间窗口导致）"
        os.makedirs("output", exist_ok=True)
        with open(DEBUG_FILE, "w", encoding="utf-8") as f:
            json.dump(debug_info, f, ensure_ascii=False, indent=2)
        if debug:
            return debug_info
        return None

    second_level_links = []
    first_level_blocked_count = 0
    first_level_browser_fallback_count = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox"])
        detail_page = browser.new_page()

        for item in match_links:
            candidate_data_plays = []
            try:
                res = requests.get(item["href"], headers=headers, timeout=12)
                if res.status_code == 200:
                    inner = BeautifulSoup(res.text, "html.parser")
                    candidate_data_plays.extend(pick_hd_play_links_from_soup(inner))
                else:
                    first_level_blocked_count += 1
            except Exception:
                first_level_blocked_count += 1

            # requests 抓不到时，用浏览器渲染页面后再提取
            if not candidate_data_plays:
                try:
                    first_level_browser_fallback_count += 1
                    detail_page.goto(item["href"], wait_until="domcontentloaded", timeout=15000)
                    links = detail_page.evaluate(
                        """
                        () => Array.from(document.querySelectorAll('a[data-play]'))
                            .map(a => ({
                                label: (a.textContent || '').trim(),
                                play: (a.getAttribute('data-play') || '').trim()
                            }))
                        """
                    )
                    for x in links:
                        play = x.get("play", "")
                        label = x.get("label", "")
                        if play.startswith("/play/") and ("高清直播" in label or "蓝光" in label):
                            candidate_data_plays.append(play)
                except Exception:
                    continue

            for data_play in set(candidate_data_plays):
                second_level_links.append(
                    {
                        **item,
                        "play_url": f"http://play.sportsteam368.com{data_play}",
                    }
                )

        debug_info["second_level_count"] = len(second_level_links)
        debug_info["second_level_samples"] = second_level_links[:5]
        debug_info["first_level_blocked_count"] = first_level_blocked_count
        debug_info["first_level_browser_fallback_count"] = first_level_browser_fallback_count

        extracted_entries = []
        existing_entries = load_existing_entries()
        existing_keys = {
            (
                item.get("league", ""),
                item.get("time", ""),
                item.get("home", ""),
                item.get("away", ""),
                item.get("id", ""),
            )
            for item in existing_entries
            if item.get("id")
        }
        seen = set(existing_keys)
        page = browser.new_page()

        for item in second_level_links:
            req_list = []
            def req_collector(request):
                req_list.append(request.url)

            page.on("request", req_collector)
            try:
                page.goto(item["play_url"], wait_until="networkidle", timeout=15000)
            except Exception:
                page.remove_listener("request", req_collector)
                continue

            page.remove_listener("request", req_collector)
            for req_url in req_list:
                if "paps.html?id=" not in req_url:
                    continue
                extracted_id = req_url.split("paps.html?id=")[-1]
                entry_key = (item["league"], item["time"], item["home"], item["away"], extracted_id)
                if entry_key in seen:
                    continue
                seen.add(entry_key)
                extracted_entries.append(
                    {
                        "league": item["league"],
                        "time": item["time"],
                        "home": item["home"],
                        "away": item["away"],
                        "id": extracted_id,
                        "match_datetime": item.get("match_datetime", ""),
                    }
                )
            
        browser.close()

    os.makedirs("output", exist_ok=True)
    existing_entries = load_existing_entries()
    final_entries = merge_and_filter_entries(existing_entries, extracted_entries, now, tz)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        for item in final_entries:
            f.write(item["id"] + "\n")

    with open(ENTRY_FILE, "w", encoding="utf-8") as f:
        json.dump(final_entries, f, ensure_ascii=False, indent=2)

    debug_info["extracted_count"] = len(extracted_entries)
    debug_info["stored_count"] = len(final_entries)
    debug_info["keep_window_start"] = build_keep_window(now)[0].isoformat()
    debug_info["keep_window_end"] = build_keep_window(now)[1].isoformat()
    debug_info["extracted_samples"] = extracted_entries[:5]
    with open(DEBUG_FILE, "w", encoding="utf-8") as f:
        json.dump(debug_info, f, ensure_ascii=False, indent=2)

    print(f"任务完成，共提取 {len(extracted_entries)} 条 ID 对应记录。")
    if debug:
        return debug_info
    return None


# ==========================================
# 统一的播放列表生成逻辑 (支持 M3U 和 TXT)
# ==========================================
def generate_playlist(fmt="m3u", mode="clean"):
    if not os.path.exists(ENTRY_FILE):
        return "请稍后再试，爬虫尚未生成数据"

    with open(ENTRY_FILE, "r", encoding="utf-8") as f:
        entries = json.load(f)

    target_key = b"ABCDEFGHIJKLMNOPQRSTUVWX"

    if fmt == "m3u":
        content = "#EXTM3U\n"
    else:
        content = "体育直播,#genre#\n"

    for entry in entries:
        raw_id = entry.get("id", "")
        if not raw_id:
            continue

        try:
            decoded_id = urllib.parse.unquote(raw_id)
            pad = 4 - (len(decoded_id) % 4)
            if pad != 4:
                decoded_id += "=" * pad

            bin_data = base64.b64decode(decoded_id)
            decrypted_bytes = xxtea_decrypt(bin_data, target_key)
            if not decrypted_bytes:
                continue

            json_str = decrypted_bytes.decode("utf-8", errors="ignore")
            data = json.loads(json_str)
            if "url" not in data:
                continue

            stream_url = data["url"]
            if mode == "plus":
                stream_url = f"{stream_url}|Referer="

            group_title = f"JRS{entry.get('league', '直播')}"
            channel_name = f"{entry.get('time', '')} {entry.get('home', '')}vs{entry.get('away', '')}".strip()

            if fmt == "m3u":
                content += f'#EXTINF:-1 group-title="{group_title}",{channel_name}\n{stream_url}\n'
            else:
                content += f"{channel_name},{stream_url}\n"
        except Exception:
            continue

    return content


# ==========================================
# Web 接口
# ==========================================
@app.route("/")
def index():
    return jsonify(
        {
            "status": "running",
            "last_run_time": LAST_RUN_TIME,
            "source": SOURCE_JS_URL,
            "endpoints": ["/ids", "/m3u", "/m3u_plus", "/txt", "/txt_plus", "/debug/run", "/debug/last"],
        }
    )


@app.route("/ids")
def ids():
    if not os.path.exists(ENTRY_FILE):
        return jsonify([])
    with open(ENTRY_FILE, "r", encoding="utf-8") as f:
        return jsonify(json.load(f))


@app.route("/debug/run")
def debug_run():
    result = scrape_job(debug=True, ignore_time_filter=True)
    return jsonify(result or {})


@app.route("/debug/last")
def debug_last():
    if not os.path.exists(DEBUG_FILE):
        return jsonify({"error": "暂无调试信息，请先访问 /debug/run"})
    with open(DEBUG_FILE, "r", encoding="utf-8") as f:
        return jsonify(json.load(f))


@app.route("/m3u")
def get_m3u_clean():
    return Response(
        generate_playlist("m3u", "clean"),
        mimetype="text/plain; charset=utf-8",
        headers={"Access-Control-Allow-Origin": "*"},
    )


@app.route("/m3u_plus")
def get_m3u_plus():
    return Response(
        generate_playlist("m3u", "plus"),
        mimetype="text/plain; charset=utf-8",
        headers={"Access-Control-Allow-Origin": "*"},
    )


@app.route("/txt")
def get_txt_clean():
    return Response(
        generate_playlist("txt", "clean"),
        mimetype="text/plain; charset=utf-8",
        headers={"Access-Control-Allow-Origin": "*"},
    )


@app.route("/txt_plus")
def get_txt_plus():
    return Response(
        generate_playlist("txt", "plus"),
        mimetype="text/plain; charset=utf-8",
        headers={"Access-Control-Allow-Origin": "*"},
    )


if __name__ == "__main__":
    scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    scheduler.add_job(scrape_job, "interval", minutes=30, next_run_time=datetime.now())
    scheduler.start()
    app.run(host="0.0.0.0", port=5000, use_reloader=False)
