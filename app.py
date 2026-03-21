import os
import requests
from bs4 import BeautifulSoup
import base64
import re
from datetime import datetime
import pytz
from playwright.sync_api import sync_playwright
from flask import Flask, jsonify, Response
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)
OUTPUT_FILE = 'output/extracted_ids.txt'
LAST_RUN_TIME = "尚未执行"

def scrape_job():
    global LAST_RUN_TIME
    tz = pytz.timezone('Asia/Shanghai')
    now = datetime.now(tz)
    LAST_RUN_TIME = now.strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{LAST_RUN_TIME}] 开始执行抓取任务...")
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }
    
    # 1. 访问主页，筛选前后3小时内的比赛
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

    print(f"找到 {len(links_to_visit)} 个符合时间窗口的比赛链接。")
    
    # 2. 提取并解码 Base64 真实播放页链接
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
            print(f"解析比赛页面失败 {link}: {e}")

    # 3. 使用 Playwright 模拟浏览器截获
    final_ids = []
    with sync_playwright() as p:
        # 必须加 --no-sandbox，否则在 Docker 中会报错
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
            except Exception as e:
                print(f"抓取动态ID超时或跳过 {url}")
        
        browser.close()

    # 4. 汇总写入文件
    os.makedirs('output', exist_ok=True)
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        for fid in set(final_ids):
            f.write(fid + '\n')
    print(f"任务完成，共保存 {len(set(final_ids))} 个独立 ID。")

# 配置 Flask 路由
@app.route('/')
def index():
    """主页，显示系统状态"""
    return jsonify({
        "status": "running",
        "last_run_time": LAST_RUN_TIME,
        "message": "访问 /ids 获取最新提取的 ID 列表"
    })

@app.route('/ids')
def get_ids():
    """读取并返回文件中的 ID"""
    if not os.path.exists(OUTPUT_FILE):
        return jsonify({"error": "尚未生成数据，请稍后再试"}), 404
        
    with open(OUTPUT_FILE, 'r', encoding='utf-8') as f:
        ids = [line.strip() for line in f.readlines() if line.strip()]
    
    return jsonify({
        "count": len(ids),
        "update_time": LAST_RUN_TIME,
        "ids": ids
    })

if __name__ == "__main__":
    # 配置并启动后台定时任务
    scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    # next_run_time 设置为当前时间，确保启动服务时立刻执行一次爬虫
    scheduler.add_job(scrape_job, 'interval', hours=1, next_run_time=datetime.now())
    scheduler.start()
    
    # 启动 Flask Web 服务
    # 监听 0.0.0.0 确保外网可以访问，端口使用 5000
    app.run(host='0.0.0.0', port=5000, use_reloader=False)
