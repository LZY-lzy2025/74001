import os
import requests
from bs4 import BeautifulSoup
import base64
import re
import urllib.parse
import json
from datetime import datetime
import pytz
from playwright.sync_api import sync_playwright
from flask import Flask, jsonify, Response
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)
OUTPUT_FILE = 'output/extracted_ids.txt'
LAST_RUN_TIME = "尚未执行"

# ==========================================
# 核心：内置轻量级 XXTEA 解密算法
# ==========================================
def str2long(s, w):
    v = []
    for i in range(0, len(s), 4):
        v0 = s[i]
        v1 = s[i+1] if i+1 < len(s) else 0
        v2 = s[i+2] if i+2 < len(s) else 0
        v3 = s[i+3] if i+3 < len(s) else 0
        v.append(v0 | (v1 << 8) | (v2 << 16) | (v3 << 24))
    if w:
        v.append(len(s))
    return v

def long2str(v, w):
    vl = len(v)
    if vl == 0: return b""
    n = (vl - 1) << 2
    if w:
        m = v[-1]
        if (m < n - 3) or (m > n): return None
        n = m
    s = bytearray()
    for i in range(vl):
        s.append(v[i] & 0xff)
        s.append((v[i] >> 8) & 0xff)
        s.append((v[i] >> 16) & 0xff)
        s.append((v[i] >> 24) & 0xff)
    return bytes(s[:n]) if w else bytes(s)

def xxtea_decrypt(data, key):
    if not data: return b""
    v = str2long(data, False)
    k = str2long(key, False)
    if len(k) < 4:
        k.extend([0] * (4 - len(k)))
    n = len(v) - 1
    if n < 1: return b""
    
    z = v[n]
    y = v[0]
    delta = 0x9E3779B9
    q = 6 + 52 // (n + 1)
    sum_val = (q * delta) & 0xffffffff
    
    while sum_val != 0:
        e = (sum_val >> 2) & 3
        for p in range(n, 0, -1):
            z = v[p - 1]
            mx = (((z >> 5) ^ (y << 2)) + ((y >> 3) ^ (z << 4))) ^ ((sum_val ^ y) + (k[(p & 3) ^ e] ^ z))
            y = v[p] = (v[p] - mx) & 0xffffffff
        z = v[n]
        mx = (((z >> 5) ^ (y << 2)) + ((y >> 3) ^ (z << 4))) ^ ((sum_val ^ y) + (k[(0 & 3) ^ e] ^ z))
        y = v[0] = (v[0] - mx) & 0xffffffff
        sum_val = (sum_val - delta) & 0xffffffff
        
    return long2str(v, True)

# ==========================================
# 爬虫任务逻辑
# ==========================================
def scrape_job():
    global LAST_RUN_TIME
    tz = pytz.timezone('Asia/Shanghai')
    now = datetime.now(tz)
    LAST_RUN_TIME = now.strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{LAST_RUN_TIME}] 开始执行抓取任务...")
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }
    
    try:
        res = requests.get('https://www.74001.tv', headers=headers, timeout=10)
        soup = BeautifulSoup(res.text, 'html.parser')
    except Exception as e:
        print(f"获取主页失败: {e}")
        return

    links_to_visit = []
    for a in soup.select('a.clearfix'):
        href = a.get('href')
        time_str = a.get('t-nzf-o')
        if href and '/bofang/' in href and time_str:
            try:
                if len(time_str) == 10:
                    time_str += " 00:00:00"
                match_time = tz.localize(datetime.strptime(time_str, '%Y-%m-%d %H:%M:%S'))
                diff_hours = abs((now - match_time).total_seconds()) / 3600
                if diff_hours <= 3:
                    match_id = href.split('/')[-1]
                    links_to_visit.append(f"https://www.74001.tv/live/{match_id}")
            except Exception:
                continue

    play_urls = []
    for link in set(links_to_visit):
        try:
            res = requests.get(link, headers=headers, timeout=10)
            soup = BeautifulSoup(res.text, 'html.parser')
            for dd in soup.select('dd[nz-g-c]'):
                b64_str = dd.get('nz-g-c')
                if b64_str:
                    decoded = base64.b64decode(b64_str).decode('utf-8', errors='ignore')
                    m = re.search(r'ftp:\*\*(.*?)(?:::|$)', decoded)
                    if m:
                        raw_url = m.group(1)
                        url = 'http://' + raw_url.replace('!', '.').replace('&nbsp', 'com').replace('*', '/')
                        play_urls.append(url)
        except Exception as e:
            print(f"解析页面失败 {link}: {e}")

    final_ids = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-setuid-sandbox'])
        page = browser.new_page()
        for url in set(play_urls):
            try:
                requests_list = []
                page.on("request", lambda request: requests_list.append(request.url))
                page.goto(url, wait_until='networkidle', timeout=15000)
                for req_url in requests_list:
                    if 'paps.html?id=' in req_url:
                        extracted_id = req_url.split('paps.html?id=')[-1]
                        final_ids.append(extracted_id)
                        print(f"成功截获 ID: {extracted_id[:20]}...")
                        break
            except Exception:
                continue
        browser.close()

    os.makedirs('output', exist_ok=True)
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        for fid in set(final_ids):
            f.write(fid + '\n')
    print(f"任务完成，共保存 {len(set(final_ids))} 个独立 ID。")

# ==========================================
# 提取 M3U 生成逻辑的通用函数
# ==========================================
def generate_m3u(mode="clean"):
    if not os.path.exists(OUTPUT_FILE):
        return "请稍后再试，爬虫尚未生成数据"
        
    with open(OUTPUT_FILE, 'r', encoding='utf-8') as f:
        ids = [line.strip() for line in f.readlines() if line.strip()]
    
    target_key = b"ABCDEFGHIJKLMNOPQRSTUVWX"
    m3u_content = "#EXTM3U\n"
    index = 1
    fake_referer = "https://www.74001.tv/"
    
    for raw_id in ids:
        try:
            if not raw_id: continue
            
            decoded_id = urllib.parse.unquote(raw_id)
            pad = 4 - (len(decoded_id) % 4)
            if pad != 4: decoded_id += "=" * pad
                
            bin_data = base64.b64decode(decoded_id)
            decrypted_bytes = xxtea_decrypt(bin_data, target_key)
            
            if decrypted_bytes:
                json_str = decrypted_bytes.decode('utf-8', errors='ignore')
                data = json.loads(json_str)
                
                if 'url' in data:
                    channel_name = data.get('name') or data.get('title') or f"74体育 直播 {index}"
                    raw_stream_url = data["url"]
                    
                    if mode == "plus":
                        # 精简防盗链：只加 Referer，去掉所有空格和多余字符，防止播放器解析崩溃
                        stream_url = f"{raw_stream_url}|Referer={fake_referer}"
                    else:
                        # 绝对纯净版：没有任何后缀
                        stream_url = raw_stream_url
                    
                    m3u_content += f'#EXTINF:-1 group-title="体育直播",{channel_name}\n{stream_url}\n'
                    index += 1
        except Exception as e:
            continue
            
    return m3u_content

# ==========================================
# Web 接口
# ==========================================
@app.route('/')
def index():
    return jsonify({
        "status": "running",
        "last_run_time": LAST_RUN_TIME,
        "endpoints": ["/ids", "/m3u (纯净原版)", "/m3u_plus (带精简Referer版)"]
    })

@app.route('/ids')
def get_ids():
    if not os.path.exists(OUTPUT_FILE):
        return jsonify({"error": "尚未生成数据"}), 404
    with open(OUTPUT_FILE, 'r', encoding='utf-8') as f:
        ids = [line.strip() for line in f.readlines() if line.strip()]
    return jsonify({"count": len(ids), "update_time": LAST_RUN_TIME, "ids": ids})

@app.route('/m3u')
def get_m3u_clean():
    """纯净版 M3U，没有任何防盗链后缀，适合容易崩溃的播放器"""
    return Response(
        generate_m3u(mode="clean"), 
        mimetype='text/plain; charset=utf-8',
        headers={"Access-Control-Allow-Origin": "*"}
    )

@app.route('/m3u_plus')
def get_m3u_plus():
    """精简防盗链版 M3U，只添加安全的 Referer，去掉了导致断链的空格"""
    return Response(
        generate_m3u(mode="plus"), 
        mimetype='text/plain; charset=utf-8',
        headers={"Access-Control-Allow-Origin": "*"}
    )

if __name__ == "__main__":
    scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    scheduler.add_job(scrape_job, 'interval', hours=1, next_run_time=datetime.now())
    scheduler.start()
    app.run(host='0.0.0.0', port=5000, use_reloader=False)
