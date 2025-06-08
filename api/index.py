import os
from flask import Flask, request, jsonify, Response
import json
import aiohttp
import asyncio
import logging
import time
from fake_useragent import UserAgent

app = Flask(__name__)

# 配置参数
COOKIES_FILE = 'cookies.txt'
REQUEST_TIMEOUT = 30
MAX_RETRIES = 3
RETRY_DELAY = 2
PORT = 3000

# 日志设置
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# UserAgent实例，用于生成随机User-Agent头
ua = UserAgent()

def get_random_headers():
    # 生成带随机User-Agent和固定Referer的请求头
    headers = {
        'User-Agent': ua.random,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
        'Accept-Encoding': 'gzip, deflate, br',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'none',
        'Sec-Fetch-User': '?1',
        'Cache-Control': 'max-age=0',
        'Referer': 'https://terafileshare.com/'  # 你可以根据实际调整
    }
    return headers

def load_cookies():
    # 从本地cookies.txt文件读取cookies，返回字典
    cookies_dict = {}
    if os.path.exists(COOKIES_FILE):
        with open(COOKIES_FILE, 'r') as f:
            for line in f:
                if not line.strip() or line.startswith('#'):
                    continue
                parts = line.strip().split('\t')
                if len(parts) >= 7:
                    cookies_dict[parts[5]] = parts[6]
    return cookies_dict

def find_between(string, start, end):
    # 从string中截取介于start和end之间的子串
    try:
        start_index = string.find(start) + len(start)
        end_index = string.find(end, start_index)
        return string[start_index:end_index] if start_index >= len(start) and end_index != -1 else None
    except Exception:
        return None

async def make_request(session, url, method='GET', headers=None, params=None, allow_redirects=True):
    retry_count = 0
    last_exception = None
    
    while retry_count < MAX_RETRIES:
        try:
            current_headers = headers or get_random_headers()
            async with session.request(
                method,
                url,
                headers=current_headers,
                params=params,
                allow_redirects=allow_redirects,
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
            ) as response:
                if response.status == 403:
                    logger.warning(f"Blocked by server (403), retrying... (attempt {retry_count + 1})")
                    retry_count += 1
                    await asyncio.sleep(RETRY_DELAY * (retry_count + 1))
                    continue
                response.raise_for_status()
                return response
        except Exception as e:
            last_exception = e
            logger.warning(f"Request error on attempt {retry_count + 1}: {str(e)}")
            retry_count += 1
            await asyncio.sleep(RETRY_DELAY * (retry_count + 1))
    
    raise Exception(f"Max retries exceeded. Last error: {str(last_exception)}")

async def fetch_download_link_async(url):
    try:
        cookies = load_cookies()
        if not cookies:
            raise Exception("No cookies found. Please provide valid cookies.")
            
        async with aiohttp.ClientSession(cookies=cookies) as session:
            # 第一次请求，拿页面内容
            response = await make_request(session, url)
            response_data = await response.text()
            
            # 提取jsToken和dplogid
            js_token = find_between(response_data, 'fn%28%22', '%22%29')
            log_id = find_between(response_data, 'dp-logid=', '&')
            
            if not js_token or not log_id:
                raise Exception("Could not extract required tokens from the page")
            
            # 取最终url中的surl参数
            request_url = str(response.url)
            surl = request_url.split('surl=')[1] if 'surl=' in request_url else None
            if not surl:
                raise Exception("Could not extract surl from URL")
            
            # 组装API请求参数
            params = {
                'app_id': '250528',
                'web': '1',
                'channel': 'dubox',
                'clienttype': '0',
                'jsToken': js_token,
                'dplogid': log_id,
                'page': '1',
                'num': '20',
                'order': 'time',
                'desc': '1',
                'site_referer': request_url,
                'shorturl': surl,
                'root': '1'
            }
            
            # 第二次请求，获取文件列表json
            list_response = await make_request(
                session,
                'https://www.1024tera.com/share/list',
                params=params
            )
            list_data = await list_response.json()
            
            if 'list' not in list_data or not list_data['list']:
                raise Exception("No files found in the shared link")
            
            # 如果第一个是目录，进一步获取目录内文件
            if list_data['list'][0]['isdir'] == "1":
                dir_params = params.copy()
                dir_params.update({
                    'dir': list_data['list'][0]['path'],
                    'order': 'asc',
                    'by': 'name',
                    'dplogid': log_id
                })
                dir_params.pop('desc', None)
                dir_params.pop('root', None)
                
                dir_response = await make_request(
                    session,
                    'https://www.1024tera.com/share/list',
                    params=dir_params
                )
                dir_data = await dir_response.json()
                
                if 'list' not in dir_data or not dir_data['list']:
                    raise Exception("No files found in the directory")
                
                return dir_data['list']
            
            return list_data['list']
    
    except Exception as e:
        logger.error(f"Error in fetch_download_link_async: {str(e)}")
        raise

async def get_direct_link(session, dlink):
    try:
        # 先尝试HEAD请求获取重定向Location
        try:
            response = await make_request(
                session,
                dlink,
                method='HEAD',
                allow_redirects=False
            )
            if 300 <= response.status < 400:
                return response.headers.get('Location', dlink)
        except Exception:
            pass
        
        # HEAD失败则用GET请求
        response = await make_request(
            session,
            dlink,
            method='GET',
            allow_redirects=False
        )
        if 300 <= response.status < 400:
            return response.headers.get('Location', dlink)
        
        return dlink
    except Exception as e:
        logger.warning(f"Could not get direct link: {str(e)}")
        return dlink

async def get_formatted_size(size_bytes):
    try:
        size_bytes = int(size_bytes)
        if size_bytes >= 1024 * 1024 * 1024:
            return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"
        elif size_bytes >= 1024 * 1024:
            return f"{size_bytes / (1024 * 1024):.2f} MB"
        elif size_bytes >= 1024:
            return f"{size_bytes / 1024:.2f} KB"
        return f"{size_bytes} bytes"
    except Exception:
        return "Unknown size"

async def process_file(session, file_data):
    try:
        direct_link = await get_direct_link(session, file_data['dlink'])
        
        return {
            "file_name": file_data.get("server_filename"),
            "size": await get_formatted_size(file_data.get("size", 0)),
            "size_bytes": file_data.get("size", 0),
            "download_url": file_data['dlink'],
            "direct_download_url": direct_link,
            "is_directory": file_data.get("isdir", "0") == "1",
            "modify_time": file_data.get("server_mtime"),
            "thumbnails": file_data.get("thumbs", {})
        }
    except Exception as e:
        logger.error(f"Error processing file: {str(e)}")
        return None

@app.route('/api', methods=['GET'])
async def api_handler():
    start_time = time.time()
    try:
        url = request.args.get('url')
        if not url:
            return jsonify({
                "status": "error",
                "message": "URL parameter is required. Developed by @Farooq_is_king. Join @OPLEECH_WD for updates.",
                "usage": "/api?url=YOUR_TERABOX_SHARE_URL"
            }), 400
        
        logger.info(f"Processing URL: {url}")
        
        files = await fetch_download_link_async(url)
        if not files:
            return jsonify({
                "status": "error",
                "message": "No files found in the shared link",
                "url": url
            }), 404
        
        async with aiohttp.ClientSession(cookies=load_cookies()) as session:
            results = []
            for file in files:
                processed = await process_file(session, file)
                if processed:
                    results.append(processed)
            
            if not results:
                return jsonify({
                    "status": "error",
                    "message": "Could not process any files",
                    "url": url
                }), 500
            
            return jsonify({
                "status": "success",
                "url": url,
                "files": results,
                "processing_time": f"{time.time() - start_time:.2f} seconds",
                "file_count": len(results)
            })
    
    except Exception as e:
        logger.error(f"API error: {str(e)}")
        return jsonify({
            "status": "error",
            "message": str(e),
            "url": url or "Not provided"
        }), 500

@app.route('/')
def home():
    data = {
        "status": "Running ✅",
        "developer": "@Farooq_is_king",
        "channel": "@Opleech_WD",
        "endpoints": {
            "/api": "GET with ?url=TERABOX_SHARE_URL parameter",
            "/health": "Service health check"
        }
    }
    return Response(json.dumps(data, ensure_ascii=False), mimetype='application/json')

@app.route('/health')
def health_check():
    data = {
        "status": "healthy",
        "developer": "@Farooq_is_king",
        "channel": "@Opleech_WD"
    }
    return Response(json.dumps(data, ensure_ascii=False), mimetype='application/json')

if __name__ == '__main__':
    port = int(os.environ.get("PORT", PORT))
    # 线程模式启动，支持异步视图
    app.run(host='0.0.0.0', port=port, threaded=True)
