import logging
import logging.handlers
import os
import random
import time
import schedule
import sys
import re
import json
import hashlib
import math
import base64
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

DEFAULT_TIMEZONE = "Asia/Shanghai"


def get_app_timezone_name():
    return (os.getenv("TZ", DEFAULT_TIMEZONE) or DEFAULT_TIMEZONE).strip()


def get_app_timezone():
    tz_name = get_app_timezone_name()
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        logger.warning(f"未找到时区 '{tz_name}'，回退为 {DEFAULT_TIMEZONE}")
        return timezone(timedelta(hours=8), name=DEFAULT_TIMEZONE)


APP_TIMEZONE = get_app_timezone()


def now_local():
    return datetime.now(APP_TIMEZONE)


def configure_process_timezone():
    tz_name = get_app_timezone_name()
    os.environ["TZ"] = tz_name
    if hasattr(time, "tzset"):
        try:
            time.tzset()
        except Exception as exc:
            logger.warning(f"设置进程时区失败: {exc}")


def apply_browser_timezone(driver):
    tz_name = get_app_timezone_name()
    try:
        driver.execute_cdp_cmd("Emulation.setTimezoneOverride", {
            "timezoneId": tz_name
        })
        logger.info(f"浏览器时区已设置为: {tz_name}")
    except Exception as exc:
        logger.warning(f"设置浏览器时区失败: {exc}")


selenium_modules = None


def import_selenium_modules():
    global selenium_modules
    if selenium_modules is None:
        from selenium import webdriver
        from selenium.webdriver import ActionChains
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
        from selenium.webdriver.chrome.webdriver import WebDriver
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.wait import WebDriverWait
        from selenium.common import TimeoutException

        selenium_modules = {
            'webdriver': webdriver,
            'ActionChains': ActionChains,
            'Options': Options,
            'Service': Service,
            'WebDriver': WebDriver,
            'By': By,
            'EC': EC,
            'WebDriverWait': WebDriverWait,
            'TimeoutException': TimeoutException
        }
    return selenium_modules


def unload_selenium_modules():
    global selenium_modules
    if selenium_modules is not None:
        modules_to_remove = [
            'selenium',
            'selenium.webdriver',
            'selenium.webdriver.chrome',
            'selenium.webdriver.chrome.options',
            'selenium.webdriver.chrome.service',
            'selenium.webdriver.chrome.webdriver',
            'selenium.webdriver.common',
            'selenium.webdriver.common.by',
            'selenium.webdriver.support',
            'selenium.webdriver.support.expected_conditions',
            'selenium.webdriver.support.wait',
            'selenium.common'
        ]

        for module in modules_to_remove:
            if module in sys.modules:
                del sys.modules[module]

        selenium_modules = None


def setup_logging():
    configure_process_timezone()

    log_dir = "logs"
    os.makedirs(log_dir, exist_ok=True)

    log_file = os.path.join(log_dir, "chmlfrp.log")
    file_handler = logging.handlers.TimedRotatingFileHandler(
        log_file,
        when='midnight',
        interval=1,
        backupCount=7,
        encoding='utf-8'
    )

    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

    cleanup_old_logs(log_dir, days=7)

    return root_logger


class NotificationProvider:
    MAX_BYTES = 0
    CONTENT_KEYS = []

    def send(self, title, context):
        raise NotImplementedError

    def select_content(self, context, max_bytes_override=None):
        limit = max_bytes_override if max_bytes_override is not None else self.MAX_BYTES

        for key in self.CONTENT_KEYS:
            content = context.get(key, '')
            if not content:
                continue
            byte_size = len(content.encode('utf-8'))
            if limit == 0 or byte_size <= limit:
                if key != self.CONTENT_KEYS[0]:
                    logging.info(f"{self.__class__.__name__}: 内容降级到 {key} ({byte_size} bytes)")
                return content

        last_key = self.CONTENT_KEYS[-1] if self.CONTENT_KEYS else ''
        last_content = context.get(last_key, '')
        if last_content and limit > 0:
            logging.warning(f"{self.__class__.__name__}: 所有内容版本均超限，执行安全截断")
            return self._safe_truncate(last_content, limit)
        return last_content

    @staticmethod
    def _safe_truncate(content, max_bytes):
        encoded = content.encode('utf-8')
        if len(encoded) <= max_bytes:
            return content
        suffix = '\n\n... [内容已截断]'
        suffix_bytes = len(suffix.encode('utf-8'))
        truncated = encoded[:max_bytes - suffix_bytes]
        return truncated.decode('utf-8', errors='ignore') + suffix


class PushPlusProvider(NotificationProvider):
    MAX_BYTES = 90_000
    FALLBACK_MAX_BYTES = 18_000
    CONTENT_KEYS = ['html_full', 'html_lite', 'summary_html']

    def __init__(self, token):
        self.token = token

    def send(self, title, context):
        import requests
        url = 'http://www.pushplus.plus/send'

        content = self.select_content(context)
        success = self._do_send(requests, url, title, content)

        if not success:
            logging.info("PushPlus: 推送失败，降级到实名用户限额 (2万字) 重试")
            content = self.select_content(context, max_bytes_override=self.FALLBACK_MAX_BYTES)
            success = self._do_send(requests, url, title, content)

        return success

    def _do_send(self, requests, url, title, content):
        data = {
            "token": self.token,
            "title": title,
            "content": content,
            "template": "html"
        }
        try:
            logging.info(f"Sending PushPlus notification: {title} ({len(content.encode('utf-8'))} bytes)")
            response = requests.post(url, json=data, timeout=10)
            result = response.json()
            if result.get('code') == 200:
                logging.info("PushPlus notification sent successfully")
                return True
            else:
                logging.error(f"PushPlus notification failed: {result.get('msg')}")
                return False
        except Exception as e:
            logging.error(f"Error sending PushPlus notification: {e}")
            return False


class WXPusherProvider(NotificationProvider):
    MAX_BYTES = 36_000
    CONTENT_KEYS = ['html_full', 'html_lite', 'summary_html']

    def __init__(self, app_token, uids=None, topic_ids=None):
        self.app_token = app_token
        if uids:
            self.uids = uids if isinstance(uids, list) else [uid.strip() for uid in str(uids).split(',') if uid.strip()]
        else:
            self.uids = []
        if topic_ids:
            self.topic_ids = topic_ids if isinstance(topic_ids, list) else [tid.strip() for tid in str(topic_ids).split(',') if tid.strip()]
        else:
            self.topic_ids = []

    def send(self, title, context):
        import requests
        content = self.select_content(context)
        url = 'https://wxpusher.zjiecode.com/api/send/message'
        data = {
            "appToken": self.app_token,
            "content": content,
            "summary": title,
            "contentType": 2,
            "uids": self.uids,
            "topicIds": self.topic_ids
        }
        try:
            target_desc = f"UIDs: {len(self.uids)}" if self.uids else ""
            if self.topic_ids:
                target_desc += (" & " if target_desc else "") + f"Topics: {len(self.topic_ids)}"

            logging.info(f"Sending WXPusher notification to {target_desc}: {title} ({len(content.encode('utf-8'))} bytes)")
            response = requests.post(url, json=data, timeout=10)
            result = response.json()
            if result.get('code') == 1000:
                logging.info("WXPusher notification sent successfully")
                return True
            else:
                logging.error(f"WXPusher notification failed: {result.get('msg')}")
                return False
        except Exception as e:
            logging.error(f"Error sending WXPusher notification: {e}")
            return False


class DingTalkProvider(NotificationProvider):
    MAX_BYTES = 18_000
    CONTENT_KEYS = ['markdown_full', 'markdown_lite', 'summary_markdown']

    def __init__(self, access_token, secret=None):
        self.access_token = access_token
        self.secret = secret

    def send(self, title, context):
        import requests
        import hmac
        import hashlib
        import base64
        import urllib.parse

        content = self.select_content(context)
        md_text = f"# {title}\n\n{content}"

        url = 'https://oapi.dingtalk.com/robot/send'
        params = {'access_token': self.access_token}

        if self.secret:
            timestamp = str(round(time.time() * 1000))
            secret_enc = self.secret.encode('utf-8')
            string_to_sign = '{}\n{}'.format(timestamp, self.secret)
            string_to_sign_enc = string_to_sign.encode('utf-8')
            hmac_code = hmac.new(secret_enc, string_to_sign_enc, digestmod=hashlib.sha256).digest()
            sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
            params['timestamp'] = timestamp
            params['sign'] = sign

        data = {
            "msgtype": "markdown",
            "markdown": {
                "title": title,
                "text": md_text
            }
        }

        try:
            logging.info(f"Sending DingTalk notification: {title} ({len(md_text.encode('utf-8'))} bytes)")
            response = requests.post(url, params=params, json=data, timeout=10)
            result = response.json()
            if result.get('errcode') == 0:
                logging.info("DingTalk notification sent successfully")
                return True
            else:
                logging.error(f"DingTalk notification failed: {result.get('errmsg')}")
                return False
        except Exception as e:
            logging.error(f"Error sending DingTalk notification: {e}")
            return False


class EmailProvider(NotificationProvider):
    MAX_BYTES = 0
    CONTENT_KEYS = ['html_email', 'html_full']

    def __init__(self, host, port, user, password, to_email):
        self.host = host
        self.port = int(port)
        self.user = user
        self.password = password
        self.to_email = to_email

    def send(self, title, context):
        import smtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart
        from email.header import Header

        content = self.select_content(context)

        try:
            message = MIMEMultipart()
            message['From'] = f"ChmlFrp-Qiandao <{self.user}>"
            message['To'] = self.to_email
            message['Subject'] = Header(title, 'utf-8')

            message.attach(MIMEText(content, 'html', 'utf-8'))

            logging.info(f"Sending Email notification to {self.to_email}")

            if self.port == 465:
                server = smtplib.SMTP_SSL(self.host, self.port)
            else:
                server = smtplib.SMTP(self.host, self.port)
                try:
                    server.starttls()
                except:
                    pass

            server.login(self.user, self.password)
            server.sendmail(self.user, [self.to_email], message.as_string())
            server.quit()

            logging.info("Email notification sent successfully")
            return True
        except Exception as e:
            logging.error(f"Error sending Email notification: {e}")
            return False


class NotificationManager:
    def __init__(self):
        self.providers = []

    def add_provider(self, provider):
        self.providers.append(provider)

    def send_all(self, title, context):
        if not self.providers:
            logging.info("No notification providers configured.")
            return

        logging.info(f"Sending notifications to {len(self.providers)} providers...")
        for provider in self.providers:
            provider.send(title, context)


def cleanup_old_logs(log_dir, days=7):
    try:
        now = time.time()
        cutoff = now - (days * 86400)

        for filename in os.listdir(log_dir):
            file_path = os.path.join(log_dir, filename)
            if os.path.isfile(file_path) and filename.startswith('chmlfrp.log.'):
                file_time = os.path.getmtime(file_path)
                if file_time < cutoff:
                    os.remove(file_path)
                    logging.info(f"已删除过期日志文件: {filename}")
    except Exception as e:
        logging.error(f"清理旧日志文件时出错: {e}")


def cleanup_logs_on_startup():
    log_dir = "logs"
    if not os.path.exists(log_dir):
        return

    try:
        log_files = [f for f in os.listdir(log_dir) if f.startswith('chmlfrp.log.')]
        total_size = sum(os.path.getsize(os.path.join(log_dir, f)) for f in log_files if os.path.isfile(os.path.join(log_dir, f)))

        if log_files:
            logging.info(f"检测到 {len(log_files)} 个历史日志文件，总大小约 {total_size / 1024 / 1024:.2f} MB")

            if len(log_files) > 10:
                logging.info("历史日志文件过多，执行清理...")
                cleanup_old_logs(log_dir, days=7)

                remaining_files = [f for f in os.listdir(log_dir) if f.startswith('chmlfrp.log.')]
                remaining_size = sum(os.path.getsize(os.path.join(log_dir, f)) for f in remaining_files if os.path.isfile(os.path.join(log_dir, f)))
                logging.info(f"清理完成，剩余 {len(remaining_files)} 个日志文件，总大小约 {remaining_size / 1024 / 1024:.2f} MB")
    except Exception as e:
        logging.error(f"启动时日志清理出错: {e}")


def setup_sigchld_handler():
    import signal

    def sigchld_handler(signum, frame):
        while True:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
                if pid == 0:
                    break
            except ChildProcessError:
                break
            except Exception:
                break

    if os.name == 'posix':
        signal.signal(signal.SIGCHLD, sigchld_handler)
        logging.info("已设置子进程自动回收机制，防止僵尸进程累积")


def cleanup_zombie_processes():
    import subprocess

    try:
        if os.name == 'posix':
            try:
                result = subprocess.run(['pgrep', '-f', 'chrome|chromedriver'],
                                      capture_output=True, text=True, timeout=5)
                if result.stdout:
                    pids = result.stdout.strip().split('\n')
                    for pid in pids:
                        if pid:
                            try:
                                subprocess.run(['pkill', '-9', '-P', pid],
                                             stderr=subprocess.DEVNULL, timeout=5)
                            except:
                                pass
                    subprocess.run(['pkill', '-9', '-f', 'chrome.*--type='],
                                 stderr=subprocess.DEVNULL, timeout=5)
                    logging.info("已清理残留的 Chrome 进程")
            except:
                pass
    except:
        pass


def get_random_user_agent(account_id: str) -> str:
    import datetime
    base_date = datetime.date(2022, 3, 29)
    base_version = 100
    days_diff = (datetime.date.today() - base_date).days
    current_ver = base_version + (days_diff // 32)

    user_agents = [
        f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{current_ver}.0.0.0 Safari/537.36",
        f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{current_ver-1}.0.0.0 Safari/537.36",
        f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{current_ver-2}.0.0.0 Safari/537.36",
        f"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:{current_ver-10}.0) Gecko/20100101 Firefox/{current_ver-10}.0",
    ]

    account_hash = hashlib.md5(account_id.encode()).hexdigest()
    seed = int(account_hash[:8], 16)
    rng = random.Random(seed)
    return rng.choice(user_agents)


def generate_fingerprint_script(account_id: str):
    account_hash = hashlib.md5(account_id.encode()).hexdigest()
    seed = int(account_hash[:8], 16)
    rng = random.Random(seed)

    webgl_vendors = [
        ("Intel Inc.", "Intel Iris Xe Graphics"),
        ("Intel Inc.", "Intel UHD Graphics 770"),
        ("NVIDIA Corporation", "NVIDIA GeForce RTX 4070/PCIe/SSE2"),
        ("NVIDIA Corporation", "NVIDIA GeForce RTX 4060/PCIe/SSE2"),
        ("AMD", "AMD Radeon RX 7600"),
    ]
    vendor, renderer = rng.choice(webgl_vendors)

    hardware_concurrency = rng.choice([4, 6, 8, 12, 16])
    device_memory = rng.choice([8, 16, 32])

    languages = [["zh-CN", "zh", "en-US", "en"], ["zh-CN", "zh"], ["en-US", "en", "zh-CN"]]
    language = rng.choice(languages)

    canvas_noise_seed = rng.randint(1, 1000000)
    audio_noise = rng.uniform(0.00001, 0.0001)
    plugins_length = rng.randint(0, 5)

    fingerprint_script = f"""
    (function() {{
        'use strict';

        const getParameterProxyHandler = {{
            apply: function(target, thisArg, args) {{
                const param = args[0];
                if (param === 37445) {{ return '{vendor}'; }}
                if (param === 37446) {{ return '{renderer}'; }}
                return Reflect.apply(target, thisArg, args);
            }}
        }};

        try {{
            WebGLRenderingContext.prototype.getParameter = new Proxy(
                WebGLRenderingContext.prototype.getParameter,
                getParameterProxyHandler
            );
        }} catch(e) {{}}

        try {{
            WebGL2RenderingContext.prototype.getParameter = new Proxy(
                WebGL2RenderingContext.prototype.getParameter,
                getParameterProxyHandler
            );
        }} catch(e) {{}}

        const noiseSeed = {canvas_noise_seed};
        function seededRandom(seed) {{
            const x = Math.sin(seed) * 10000;
            return x - Math.floor(x);
        }}

        const originalToDataURL = HTMLCanvasElement.prototype.toDataURL;
        HTMLCanvasElement.prototype.toDataURL = function(type, quality) {{
            const canvas = this;
            const ctx = canvas.getContext('2d');
            if (ctx) {{
                const imageData = ctx.getImageData(0, 0, canvas.width, canvas.height);
                const data = imageData.data;
                for (let i = 0; i < data.length; i += 4) {{
                    if (seededRandom(noiseSeed + i) < 0.01) {{
                        data[i] = data[i] ^ 1;
                        data[i+1] = data[i+1] ^ 1;
                    }}
                }}
                ctx.putImageData(imageData, 0, 0);
            }}
            return originalToDataURL.apply(this, arguments);
        }};

        Object.defineProperty(navigator, 'hardwareConcurrency', {{ get: () => {hardware_concurrency} }});
        Object.defineProperty(navigator, 'deviceMemory', {{ get: () => {device_memory} }});
        Object.defineProperty(navigator, 'languages', {{ get: () => {language} }});
        Object.defineProperty(navigator, 'language', {{ get: () => '{language[0]}' }});
        Object.defineProperty(navigator, 'plugins', {{
            get: () => ({{ length: {plugins_length}, item: () => null, namedItem: () => null, refresh: () => {{}}, [Symbol.iterator]: function* () {{}} }})
        }});
        Object.defineProperty(navigator, 'webdriver', {{ get: () => undefined }});
        window.chrome = {{ runtime: {{}}, loadTimes: function() {{}}, csi: function() {{}}, app: {{}} }};
    }})();
    """
    return fingerprint_script


def get_proxy_ip():
    import requests

    proxy_api_url = os.getenv("PROXY_API_URL", "").strip()
    if not proxy_api_url:
        return None

    try:
        time.sleep(random.uniform(0.5, 2.0))
        logger.info(f"正在从代理接口获取IP...")
        response = requests.get(proxy_api_url, timeout=10)

        if response.status_code != 200:
            logger.error(f"代理接口请求失败，状态码: {response.status_code}")
            return None

        proxy = parse_proxy_response(response.text)
        if proxy:
            logger.info(f"获取到代理IP: {proxy}")
            return proxy
        else:
            logger.error(f"代理接口返回格式无法解析: {response.text[:100]}")
            return None

    except Exception as e:
        logger.error(f"获取代理IP失败: {e}")
        return None


def parse_proxy_response(response_text):
    response_text = response_text.strip()

    try:
        data = json.loads(response_text)
        if "data" in data and isinstance(data["data"], dict):
            data = data["data"]

        if "proxy" in data:
            proxy = str(data["proxy"]).strip()
            if "://" in proxy:
                proxy = proxy.split("://")[-1]
            return proxy if ":" in proxy else None

        if "ip" in data and "port" in data:
            return f"{data['ip']}:{data['port']}"
    except:
        pass

    proxy = response_text.strip()
    if "://" in proxy:
        proxy = proxy.split("://")[-1]

    if ":" in proxy:
        parts = proxy.split(":")
        if len(parts) == 2 and parts[1].isdigit():
            return proxy
    return None


def validate_proxy(proxy, timeout=10):
    import requests

    if not proxy:
        return False

    try:
        test_proxies = {"http": f"http://{proxy}", "https": f"http://{proxy}"}
        logger.info(f"正在验证代理 {proxy} 的可用性...")
        response = requests.get("https://www.baidu.com", proxies=test_proxies, timeout=timeout)
        if response.status_code == 200:
            logger.info(f"代理 {proxy} 验证成功")
            return True
        return False
    except:
        logger.warning(f"代理 {proxy} 验证失败")
        return False


def get_screenshot_html(screenshot_path):
    if not screenshot_path or not os.path.exists(screenshot_path):
        return ""

    try:
        with open(screenshot_path, 'rb') as f:
            img_data = base64.b64encode(f.read()).decode('utf-8')
        return f'<div style="margin:10px 0;"><img src="data:image/png;base64,{img_data}" style="max-width:100%;border:1px solid #ddd;border-radius:4px;"/></div>'
    except Exception as e:
        logger.warning(f"转换截图失败: {e}")
        return ""


def generate_html_report(results, screenshot_mode='all'):
    success_count = sum(1 for r in results if r['status'])
    fail_count = sum(1 for r in results if not r['status'])
    total_count = len(results)

    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px;">
        <div style="background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);color:white;padding:20px;border-radius:10px;margin-bottom:20px;">
            <h2 style="margin:0;">🌧️ ChmlFrp 每日签到报告</h2>
            <p style="margin:5px 0 0 0;opacity:0.9;">{now_local().strftime('%Y-%m-%d %H:%M:%S')}</p>
        </div>
        <div style="display:flex;gap:10px;margin-bottom:20px;">
            <div style="flex:1;background:#f0f9eb;padding:15px;border-radius:8px;text-align:center;">
                <div style="font-size:24px;font-weight:bold;color:#67c23a;">{success_count}</div>
                <div style="font-size:12px;color:#666;">成功</div>
            </div>
            <div style="flex:1;background:#fef0f0;padding:15px;border-radius:8px;text-align:center;">
                <div style="font-size:24px;font-weight:bold;color:#f56c6c;">{fail_count}</div>
                <div style="font-size:12px;color:#666;">失败</div>
            </div>
            <div style="flex:1;background:#ecf5ff;padding:15px;border-radius:8px;text-align:center;">
                <div style="font-size:24px;font-weight:bold;color:#409eff;">{total_count}</div>
                <div style="font-size:12px;color:#666;">总计</div>
            </div>
        </div>
    """

    for i, result in enumerate(results, 1):
        status_icon = "✅" if result['status'] else "❌"
        status_color = "#67c23a" if result['status'] else "#f56c6c"
        status_text = "签到成功" if result['status'] else "签到失败"

        html += f"""
        <div style="border:1px solid #e4e7ed;border-radius:8px;padding:15px;margin-bottom:10px;">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
                <span style="font-weight:bold;">账号 {i}: {result['username']}</span>
                <span style="color:{status_color};font-weight:bold;">{status_icon} {status_text}</span>
            </div>
        """

        if result['status']:
            html += f'<div style="color:#666;font-size:14px;">积分: <b>{result.get("integral", "未知")}</b></div>'
            if result.get('msg'):
                html += f'<div style="color:#999;font-size:12px;margin-top:5px;">{result["msg"]}</div>'
        else:
            html += f'<div style="color:#f56c6c;font-size:14px;">失败原因: {result.get("msg", "未知")}</div>'

        if screenshot_mode == 'all' or (screenshot_mode == 'failed_only' and not result['status']):
            screenshot_html = get_screenshot_html(result.get('screenshot'))
            if screenshot_html:
                html += screenshot_html

        html += "</div>"

    html += '<div style="text-align:center;color:#999;font-size:12px;margin-top:20px;">ChmlFrp 自动签到工具</div></div>'
    return html


def generate_markdown_report(results, compact=False):
    success_count = sum(1 for r in results if r['status'])
    fail_count = sum(1 for r in results if not r['status'])

    md = f"## ChmlFrp 每日签到报告\n\n"
    md += f"📅 时间: {now_local().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    md += f"| 状态 | 数量 |\n|------|------|\n"
    md += f"| ✅ 成功 | {success_count} |\n| ❌ 失败 | {fail_count} |\n| 📊 总计 | {len(results)} |\n\n"

    for i, result in enumerate(results, 1):
        status_icon = "✅" if result['status'] else "❌"
        md += f"### {status_icon} 账号 {i}: {result['username']}\n\n"
        if result['status']:
            md += f"- **状态**: 签到成功\n"
            md += f"- **积分**: {result.get('integral', '未知')}\n"
            if result.get('msg'):
                md += f"- **备注**: {result['msg']}\n"
        else:
            md += f"- **状态**: 签到失败\n"
            md += f"- **原因**: {result.get('msg', '未知')}\n"
        md += "\n"

    return md


def generate_summary_report(results, fmt='html'):
    success_count = sum(1 for r in results if r['status'])
    fail_count = sum(1 for r in results if not r['status'])

    if fmt == 'html':
        html = f"<h2>ChmlFrp 签到摘要</h2><p>📅 {now_local().strftime('%Y-%m-%d %H:%M:%S')}</p>"
        html += f"<p>✅ 成功: {success_count} | ❌ 失败: {fail_count} | 📊 总计: {len(results)}</p>"
        for i, result in enumerate(results, 1):
            status_icon = "✅" if result['status'] else "❌"
            html += f"<p>{status_icon} 账号 {i}: {result['username']} - {'成功' if result['status'] else '失败: ' + result.get('msg', '未知')}</p>"
        return html
    else:
        md = f"## ChmlFrp 签到摘要\n\n📅 {now_local().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        md += f"✅ 成功: {success_count} | ❌ 失败: {fail_count} | 📊 总计: {len(results)}\n\n"
        for i, result in enumerate(results, 1):
            status_icon = "✅" if result['status'] else "❌"
            md += f"{status_icon} 账号 {i}: {result['username']} - {'成功' if result['status'] else '失败: ' + result.get('msg', '未知')}\n"
        return md


def save_screenshot(driver, account_id, status="success", error_msg=""):
    os.makedirs("temp/screenshots", exist_ok=True)

    account_hash = hashlib.md5(account_id.encode()).hexdigest()[:16]
    timestamp = now_local().strftime("%Y%m%d_%H%M%S")
    filename = f"{account_hash}_{status}_{timestamp}.png"
    filepath = os.path.join("temp", "screenshot.png")

    try:
        driver.save_screenshot(filepath)
        logger.info(f"截图已保存: {filepath}")
        return filepath
    except Exception as e:
        logger.warning(f"截图保存失败: {e}")
        return None


def cleanup_old_screenshots(screenshot_dir, days=7):
    try:
        now = time.time()
        cutoff = now - (days * 86400)

        if not os.path.exists(screenshot_dir):
            return

        for filename in os.listdir(screenshot_dir):
            file_path = os.path.join(screenshot_dir, filename)
            if os.path.isfile(file_path) and filename.endswith('.png'):
                file_time = os.path.getmtime(file_path)
                if file_time < cutoff:
                    os.remove(file_path)
                    logger.info(f"已删除过期截图: {filename}")
    except Exception as e:
        logger.error(f"清理旧截图时出错: {e}")


def init_selenium(account_id: str, proxy: str = None):
    modules = import_selenium_modules()
    webdriver = modules['webdriver']
    Options = modules['Options']

    options = Options()

    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--disable-gpu')
    options.add_argument('--disable-blink-features=AutomationControlled')
    options.add_experimental_option('excludeSwitches', ['enable-automation'])
    options.add_experimental_option('useAutomationExtension', False)
    options.add_argument('--headless=new')
    options.add_argument('--window-size=1920,1080')

    user_agent = get_random_user_agent(account_id)
    options.add_argument(f'--user-agent={user_agent}')

    if proxy:
        options.add_argument(f'--proxy-server=http://{proxy}')
        logger.info(f"已设置代理: {proxy}")

    linux = os.getenv("LINUX_MODE", "true").lower() == "true" or os.path.exists("/.dockerenv")
    if linux:
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')

    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(30)

    logger.info("Selenium 初始化完成")
    return driver


def save_cookies(driver, account_id):
    if not account_id:
        return

    os.makedirs("temp/cookies", exist_ok=True)
    account_hash = hashlib.md5(account_id.encode()).hexdigest()[:16]
    cookie_path = os.path.join("temp", "cookies", f"chmlfrp_{account_hash}.json")

    try:
        cookies = driver.get_cookies()
        with open(cookie_path, 'w', encoding='utf-8') as f:
            json.dump(cookies, f, ensure_ascii=False)
        logger.info(f"Cookie 已保存到本地")
    except Exception as e:
        logger.warning(f"保存 Cookie 失败: {e}")


def load_cookies(driver, account_id):
    if not account_id:
        return False

    account_hash = hashlib.md5(account_id.encode()).hexdigest()[:16]
    cookie_path = os.path.join("temp", "cookies", f"chmlfrp_{account_hash}.json")

    if not os.path.exists(cookie_path):
        logger.info("未找到本地 Cookie，将使用账号密码登录")
        return False

    try:
        with open(cookie_path, 'r', encoding='utf-8') as f:
            cookies = json.load(f)

        driver.get("https://panel.chmlfrp.net/")
        time.sleep(2)

        for cookie in cookies:
            if 'expiry' in cookie:
                cookie['expiry'] = int(cookie['expiry'])
            try:
                driver.add_cookie(cookie)
            except Exception:
                pass

        logger.info(f"已加载本地 Cookie")
        return True
    except Exception as e:
        logger.warning(f"加载 Cookie 失败: {e}")
        return False


def parse_accounts():
    usernames = os.getenv("CHMLFRP_USERNAME", "").split("|")
    passwords = os.getenv("CHMLFRP_PASSWORD", "").split("|")

    if len(usernames) != len(passwords):
        logger.warning("用户名和密码数量不匹配，只使用匹配的部分")
        min_len = min(len(usernames), len(passwords))
        usernames = usernames[:min_len]
        passwords = passwords[:min_len]

    accounts = [(u.strip(), p.strip()) for u, p in zip(usernames, passwords) if u.strip() and p.strip()]

    if not accounts:
        single_user = os.getenv("CHMLFRP_USERNAME", "username")
        single_pwd = os.getenv("CHMLFRP_PASSWORD", "password")
        accounts = [(single_user, single_pwd)]

    logger.info(f"检测到 {len(accounts)} 个账号")
    for i, (username, _) in enumerate(accounts, 1):
        masked_user = f"{username[:3]}***{username[-3:] if len(username) > 6 else username}"
        logger.info(f"账号 {i}: {masked_user}")

    return accounts


class EmailCodeReader:
    """IMAP 邮箱验证码读取器"""

    def __init__(self, imap_host, imap_port, email_user, email_password):
        self.imap_host = imap_host
        self.imap_port = int(imap_port)
        self.email_user = email_user
        self.email_password = email_password

    def get_verification_code(self, sender_filter=None, subject_filter=None, max_wait=120, check_interval=5):
        import imaplib
        import email
        from email.header import decode_header

        start_time = time.time()
        last_seen_uid = None

        while time.time() - start_time < max_wait:
            try:
                mail = imaplib.IMAP4_SSL(self.imap_host, self.imap_port)
                mail.login(self.email_user, self.email_password)

                try:
                    mail.select("INBOX")
                    status, messages = mail.search(None, 'UNSEEN')

                    if status != 'OK' or not messages[0]:
                        time.sleep(check_interval)
                        continue

                    email_ids = messages[0].split()
                    if not email_ids:
                        time.sleep(check_interval)
                        continue

                    latest_id = email_ids[-1]
                    if latest_id == last_seen_uid:
                        time.sleep(check_interval)
                        continue

                    last_seen_uid = latest_id

                    status, msg_data = mail.fetch(latest_id, "(RFC822)")
                    if status != 'OK':
                        time.sleep(check_interval)
                        continue

                    raw_email = msg_data[0][1]
                    msg = email.message_from_bytes(raw_email)

                    subject = self._decode_header(msg['Subject'])
                    sender = self._decode_header(msg['From'])

                    logger.info(f"找到新邮件: 来自 {sender}, 主题: {subject}")

                    if sender_filter and sender_filter.lower() not in sender.lower():
                        time.sleep(check_interval)
                        continue

                    if subject_filter and subject_filter.lower() not in subject.lower():
                        time.sleep(check_interval)
                        continue

                    body = self._get_email_body(msg)
                    code = self._extract_code(body)

                    if code:
                        logger.info(f"成功提取验证码: {code}")
                        return code

                finally:
                    mail.logout()

            except Exception as e:
                logger.warning(f"读取邮件时出错: {e}")

            time.sleep(check_interval)

        logger.error(f"等待验证码超时（{max_wait}秒）")
        return None

    @staticmethod
    def _decode_header(header_value):
        if not header_value:
            return ""
        decoded_parts = decode_header(header_value)
        result = ""
        for part, encoding in decoded_parts:
            if isinstance(part, bytes):
                try:
                    result += part.decode(encoding or 'utf-8')
                except:
                    result += part.decode('utf-8', errors='ignore')
            else:
                result += part
        return result

    @staticmethod
    def _get_email_body(msg):
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                content_disposition = str(part.get("Content-Disposition"))

                if content_type == "text/plain" and "attachment" not in content_disposition:
                    try:
                        body += part.get_payload(decode=True).decode('utf-8', errors='ignore')
                    except:
                        pass
                elif content_type == "text/html" and "attachment" not in content_disposition:
                    try:
                        body += part.get_payload(decode=True).decode('utf-8', errors='ignore')
                    except:
                        pass
        else:
            try:
                body = msg.get_payload(decode=True).decode('utf-8', errors='ignore')
            except:
                body = str(msg.get_payload())
        return body

    @staticmethod
    def _extract_code(body):
        patterns = [
            r'验证码[：:]\s*(\d{4,8})',
            r'您的验证码是[：:]\s*(\d{4,8})',
            r'邮箱验证码[：:]\s*(\d{4,8})',
            r'登录验证码[：:]\s*(\d{4,8})',
            r'(\d{6})\s*验证码',
        ]

        for pattern in patterns:
            match = re.search(pattern, body, re.IGNORECASE)
            if match:
                return match.group(1)

        match = re.search(r'\b(\d{6})\b', body)
        if match:
            return match.group(1)

        return None


class GeeTestSlider:
    """GeeTest 滑块验证码处理"""

    def __init__(self, driver, logger_adapter):
        self.driver = driver
        self.logger = logger_adapter

    def find_captcha_elements(self):
        """查找 GeeTest 验证码相关元素"""
        modules = import_selenium_modules()
        By = modules['By']

        result = {}

        try:
            iframe = self.driver.find_element(By.CSS_SELECTOR, "iframe[src*='geetest']")
            result['iframe'] = iframe
        except:
            pass

        try:
            slider = self.driver.find_element(By.CSS_SELECTOR, ".geetest_slider_button")
            result['slider'] = slider
        except:
            pass

        try:
            bg_images = self.driver.find_elements(By.CSS_SELECTOR, ".geetest_bg img")
            if bg_images:
                result['bg_images'] = bg_images
        except:
            pass

        try:
            slice_images = self.driver.find_elements(By.CSS_SELECTOR, ".geetest_slice_bg img")
            if slice_images:
                result['slice_images'] = slice_images
        except:
            pass

        return result

    def get_slider_position(self):
        """获取滑块位置"""
        modules = import_selenium_modules()
        By = modules['By']
        ActionChains = modules['ActionChains']

        try:
            slider = self.driver.find_element(By.CSS_SELECTOR, ".geetest_slider_button")
            location = slider.location
            size = slider.size
            x = location['x'] + size['width'] // 2
            y = location['y'] + size['height'] // 2
            return slider, x, y
        except Exception as e:
            self.logger.error(f"无法找到滑块: {e}")
            return None, None, None

    def get_gap_position(self, full_bg_element, slice_bg_element):
        """使用 OpenCV 计算缺口位置"""
        try:
            import cv2
            import numpy as np
        except ImportError:
            self.logger.warning("OpenCV 未安装，无法自动计算缺口位置")
            return None

        try:
            full_bg = full_bg_element.screenshot_as_png
            slice_bg = slice_bg_element.screenshot_as_png

            nparr1 = np.frombuffer(full_bg, np.uint8)
            img1 = cv2.imdecode(nparr1, cv2.IMREAD_COLOR)

            nparr2 = np.frombuffer(slice_bg, np.uint8)
            img2 = cv2.imdecode(nparr2, cv2.IMREAD_COLOR)

            if img1 is None or img2 is None:
                self.logger.error("图片解码失败")
                return None

            bg_height, bg_width = img1.shape[:2]
            self.logger.info(f"背景图尺寸: {bg_width}x{bg_height}")

            result = cv2.matchTemplate(img1, img2, cv2.TM_CCOEFF_NORMED)
            min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(result)

            self.logger.info(f"模板匹配结果: max_val={max_val:.3f}, max_loc={max_loc}")

            if max_val > 0.5:
                gap_x = max_loc[0]
                self.logger.info(f"检测到缺口位置: x={gap_x}")
                return gap_x

            return None

        except Exception as e:
            self.logger.error(f"计算缺口位置失败: {e}")
            return None

    def human_like_track(self, distance):
        """
        生成模拟人类拖拽轨迹
        使用缓动函数 + 随机抖动模拟人类行为
        """
        tracks = []
        current = 0
        mid = distance * 0.6

        while current < distance:
            if current < mid:
                move = int(random.uniform(8, 15))
            else:
                move = int(random.uniform(2, 6))

            if current + move > distance:
                move = distance - current

            current += move
            tracks.append(move)

            if random.random() < 0.3 and current < distance - 5:
                tracks.append(random.randint(-3, 3))
                current += 0

        if sum(tracks) < distance:
            tracks.append(distance - sum(tracks))

        return tracks

    def move_to_gap(self, slider_element, target_x, target_y):
        """移动滑块到缺口位置"""
        modules = import_selenium_modules()
        ActionChains = modules['ActionChains']

        try:
            ActionChains(self.driver).click_and_hold(slider_element).perform()
            time.sleep(0.2)

            tracks = self.human_like_track(target_x)
            move_y = 0

            for track in tracks:
                x_offset = track + random.randint(-2, 2)
                y_offset = random.randint(-3, 3)

                ActionChains(self.driver).move_by_offset(x_offset, y_offset).perform()
                time.sleep(random.uniform(0.01, 0.03))
                move_y += y_offset

            ActionChains(self.driver).move_by_offset(0, -move_y).perform()
            time.sleep(0.3)
            ActionChains(self.driver).release().perform()

            self.logger.info("滑块已拖拽到目标位置")
            return True

        except Exception as e:
            self.logger.error(f"拖拽滑块失败: {e}")
            return False

    def try_solve(self, max_attempts=3):
        """尝试解决滑块验证码"""
        for attempt in range(max_attempts):
            self.logger.info(f"尝试解决滑块验证码 (第 {attempt + 1}/{max_attempts} 次)")

            elements = self.find_captcha_elements()

            if not elements.get('slider'):
                self.logger.info("未检测到滑块，可能已通过或无验证码")
                return True

            slider, slider_x, slider_y = self.get_slider_position()
            if not slider:
                self.logger.warning("无法获取滑块位置")
                time.sleep(2)
                continue

            bg_images = elements.get('bg_images', [])
            slice_images = elements.get('slice_images', [])

            if len(bg_images) >= 2 and len(slice_images) >= 1:
                gap_x = self.get_gap_position(bg_images[0], slice_images[0])

                if gap_x:
                    gap_x = int(gap_x * 0.5)
                    self.logger.info(f"目标滑块距离: {gap_x}")

                    success = self.move_to_gap(slider, gap_x, slider_y)
                    if success:
                        time.sleep(2)

                        if self.is_solved():
                            self.logger.info("滑块验证通过！")
                            return True

            time.sleep(2)

        self.logger.warning(f"滑块验证失败 ({max_attempts} 次尝试)")
        return False

    def is_solved(self):
        """检查验证码是否已解决"""
        try:
            page_source = self.driver.page_source.lower()
            if '验证失败' in page_source or '验证超时' in page_source:
                return False

            elements = self.find_captcha_elements()
            if not elements.get('slider'):
                return True

            return False
        except:
            return False


def login_chmlfrp(driver, username, password, logger_adapter, timeout):
    """登录 ChmlFrp（SSO + MFA 邮箱验证码）"""
    modules = import_selenium_modules()
    By = modules['By']
    EC = modules['EC']
    WebDriverWait = modules['WebDriverWait']
    TimeoutException = modules['TimeoutException']

    wait = WebDriverWait(driver, timeout)

    logger_adapter.info("正在访问 ChmlFrp 管理面板...")
    driver.get("https://panel.chmlfrp.net/sign?redirect=/home")
    time.sleep(3)

    if "panel.chmlfrp.net" in driver.current_url and "sign" not in driver.current_url:
        logger_adapter.info("已登录状态，无需重新登录")
        return True

    logger_adapter.info("进入 SSO 登录页面")

    try:
        username_input = wait.until(EC.visibility_of_element_located((By.ID, 'username')))
    except TimeoutException:
        try:
            username_input = wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, 'input[type="text"]')))
        except TimeoutException:
            logger_adapter.error("无法找到用户名输入框")
            return False

    try:
        password_input = driver.find_element(By.ID, 'password')
    except:
        try:
            password_input = driver.find_element(By.CSS_SELECTOR, 'input[type="password"]')
        except:
            logger_adapter.error("无法找到密码输入框")
            return False

    logger_adapter.info("输入用户名和密码...")
    username_input.clear()
    username_input.send_keys(username)
    time.sleep(0.5)
    password_input.clear()
    password_input.send_keys(password)
    time.sleep(0.5)

    try:
        login_button = driver.find_element(By.CSS_SELECTOR, 'button[type="submit"]')
    except:
        try:
            login_button = driver.find_element(By.XPATH, '//button[contains(text(), "登")]')
        except:
            logger_adapter.error("无法找到登录按钮")
            return False

    logger_adapter.info("点击登录按钮...")
    login_button.click()
    time.sleep(3)

    if "/mfa" in driver.current_url or "安全验证" in driver.page_source:
        logger_adapter.info("检测到 MFA 安全验证，需要邮箱验证码")
        return handle_mfa(driver, logger_adapter, timeout)

    if "panel.chmlfrp.net" in driver.current_url:
        logger_adapter.info("登录成功，已跳转到面板")
        return True

    if "bad_credentials" in driver.current_url:
        logger_adapter.error("用户名或密码错误")
        return False

    time.sleep(5)
    if "panel.chmlfrp.net" in driver.current_url and "sign" not in driver.current_url:
        logger_adapter.info("登录成功")
        return True

    logger_adapter.error(f"登录结果未知，当前页面: {driver.current_url}")
    return False


def handle_mfa(driver, logger_adapter, timeout):
    """处理 MFA 邮箱验证码"""
    modules = import_selenium_modules()
    By = modules['By']
    EC = modules['EC']
    WebDriverWait = modules['WebDriverWait']
    TimeoutException = modules['TimeoutException']

    wait = WebDriverWait(driver, timeout)

    imap_host = os.getenv("IMAP_HOST", "imap.163.com")
    imap_port = os.getenv("IMAP_PORT", "993")
    imap_user = os.getenv("IMAP_USER", "")
    imap_password = os.getenv("IMAP_PASSWORD", "")

    if not imap_user or not imap_password:
        logger_adapter.error("未配置 IMAP 邮箱信息，无法自动读取验证码")
        return False

    try:
        send_code_button = wait.until(EC.element_to_be_clickable((By.XPATH, '//button[contains(text(), "发送验证码")]')))
        logger_adapter.info("点击发送验证码...")
        send_code_button.click()
        logger_adapter.info("验证码已发送，请查收邮箱")
    except TimeoutException:
        logger_adapter.error("无法找到发送验证码按钮")
        return False

    logger_adapter.info("正在从邮箱读取验证码（最多等待120秒）...")
    email_reader = EmailCodeReader(imap_host, imap_port, imap_user, imap_password)
    code = email_reader.get_verification_code(
        sender_filter="chmlfrp",
        subject_filter="验证码",
        max_wait=120,
        check_interval=5
    )

    if not code:
        logger_adapter.error("未能获取到邮箱验证码")
        return False

    logger_adapter.info(f"获取到验证码: {code}")

    try:
        code_input = wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, 'input[placeholder*="验证码"]')))
    except TimeoutException:
        try:
            code_input = driver.find_element(By.CSS_SELECTOR, 'input[type="text"]')
        except:
            logger_adapter.error("无法找到验证码输入框")
            return False

    logger_adapter.info("输入验证码...")
    code_input.clear()
    code_input.send_keys(code)
    time.sleep(1)

    try:
        confirm_button = driver.find_element(By.XPATH, '//button[contains(text(), "确认")]')
        logger_adapter.info("点击确认按钮...")
        confirm_button.click()
    except:
        logger_adapter.error("无法找到确认按钮")
        return False

    time.sleep(5)

    if "panel.chmlfrp.net" in driver.current_url and "sign" not in driver.current_url:
        logger_adapter.info("MFA 验证通过，登录成功！")
        return True

    if "/mfa" in driver.current_url:
        logger_adapter.warning("MFA 验证可能失败，仍在验证页面")
        return False

    logger_adapter.info(f"MFA 验证后跳转至: {driver.current_url}")
    return True


def click_sign_and_handle_geetest(driver, logger_adapter, timeout):
    """点击签到按钮并处理 GeeTest 滑块验证码"""
    modules = import_selenium_modules()
    By = modules['By']
    EC = modules['EC']
    WebDriverWait = modules['WebDriverWait']
    TimeoutException = modules['TimeoutException']

    wait = WebDriverWait(driver, timeout)

    try:
        logger_adapter.info("跳转到首页...")
        driver.get("https://panel.chmlfrp.net/home")
        time.sleep(3)
    except Exception as e:
        logger_adapter.error(f"跳转首页失败: {e}")
        return False

    try:
        logger_adapter.info("查找签到按钮...")

        sign_button_xpaths = [
            '//button[contains(text(), "签到")]',
            '//button[contains(text(), "领取")]',
            '//button[contains(text(), "每日")]',
            '//*[contains(text(), "立即签到")]',
            '//*[contains(text(), "去签到")]',
            '//*[contains(@class, "sign") and contains(@class, "button")]',
        ]

        sign_button = None
        for xpath in sign_button_xpaths:
            try:
                elements = driver.find_elements(By.XPATH, xpath)
                for elem in elements:
                    if elem.is_displayed() and elem.is_enabled():
                        sign_button = elem
                        logger_adapter.info(f"找到签到按钮: {xpath}")
                        break
                if sign_button:
                    break
            except:
                continue

        if not sign_button:
            page_text = driver.page_source
            if "已签到" in page_text or "今日已签" in page_text:
                logger_adapter.info("今日已签到")
                return True
            logger_adapter.warning("未找到签到按钮，尝试查找积分区域...")
            try:
                earn_elem = driver.find_element(By.XPATH, '//*[contains(text(), "领取奖励")]')
                sign_button = earn_elem
            except:
                pass

        if sign_button:
            logger_adapter.info("点击签到按钮...")
            sign_button.click()
            time.sleep(2)

            geetest_solver = GeeTestSlider(driver, logger_adapter)
            if geetest_solver.try_solve(max_attempts=3):
                logger_adapter.info("GeeTest 验证通过")
                time.sleep(2)
                return True
            else:
                logger_adapter.warning("GeeTest 验证可能失败，但继续尝试...")
                return True
        else:
            logger_adapter.warning("未找到签到按钮")
            return False

    except Exception as e:
        logger_adapter.error(f"签到流程异常: {e}")
        return False


def run_checkin(account_user=None, account_pwd=None):
    """执行签到任务"""
    modules = import_selenium_modules()
    webdriver = modules['webdriver']
    By = modules['By']
    EC = modules['EC']
    WebDriverWait = modules['WebDriverWait']
    TimeoutException = modules['TimeoutException']
    import subprocess

    current_user = account_user or user
    current_pwd = account_pwd or pwd
    driver = None

    masked_user = f"{current_user[:3]}***{current_user[-3:] if len(current_user) > 6 else current_user}"

    class PrefixAdapter(logging.LoggerAdapter):
        def process(self, msg, kwargs):
            return '[%s] %s' % (self.extra['prefix'], msg), kwargs

    logger_adapter = PrefixAdapter(logger, {'prefix': masked_user})

    try:
        logger_adapter.info(f"开始执行签到任务...")

        proxy = None
        proxy_api_url = os.getenv("PROXY_API_URL", "").strip()
        if proxy_api_url:
            proxy = get_proxy_ip()
            if proxy and not validate_proxy(proxy):
                logger_adapter.warning(f"代理 {proxy} 验证失败，将使用本地IP继续")
                proxy = None

        logger_adapter.info("初始化 Selenium（账号专属配置）")
        driver = init_selenium(current_user, proxy=proxy)
        apply_browser_timezone(driver)

        with open("stealth.min.js", mode="r") as f:
            js = f.read()
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": js})

        fingerprint_js = generate_fingerprint_script(current_user)
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": fingerprint_js})
        logger_adapter.info("已注入浏览器指纹脚本（账号专属指纹）")

        wait = WebDriverWait(driver, timeout)

        cookie_ok = load_cookies(driver, current_user)

        if cookie_ok:
            logger_adapter.info("尝试使用 Cookie 登录...")
            driver.get("https://panel.chmlfrp.net/home")
            time.sleep(3)

            if "panel.chmlfrp.net" in driver.current_url and "sign" not in driver.current_url:
                logger_adapter.info("Cookie 有效，免密登录成功！🎉")
            else:
                logger_adapter.info("Cookie 已失效，使用账号密码重新登录")
                login_success = login_chmlfrp(driver, current_user, current_pwd, logger_adapter, timeout)
                if not login_success:
                    screenshot_path = save_screenshot(driver, current_user, status="failure")
                    return {
                        'status': False, 'msg': '登录失败', 'integral': 0,
                        'username': masked_user, 'screenshot': screenshot_path
                    }
                save_cookies(driver, current_user)
        else:
            logger_adapter.info("使用账号密码登录")
            login_success = login_chmlfrp(driver, current_user, current_pwd, logger_adapter, timeout)
            if not login_success:
                screenshot_path = save_screenshot(driver, current_user, status="failure")
                return {
                    'status': False, 'msg': '登录失败', 'integral': 0,
                    'username': masked_user, 'screenshot': screenshot_path
                }
            save_cookies(driver, current_user)

        logger_adapter.info("登录成功，开始执行签到...")

        sign_success = click_sign_and_handle_geetest(driver, logger_adapter, timeout)

        if sign_success:
            logger_adapter.info("签到流程完成")
            screenshot_path = save_screenshot(driver, current_user, status="success")
            return {
                'status': True,
                'msg': '签到成功',
                'integral': '未知',
                'username': masked_user,
                'screenshot': screenshot_path
            }
        else:
            logger_adapter.warning("签到可能未成功完成")
            screenshot_path = save_screenshot(driver, current_user, status="failure")
            return {
                'status': False,
                'msg': '签到流程异常',
                'integral': 0,
                'username': masked_user,
                'screenshot': screenshot_path
            }

    except Exception as e:
        logger_adapter.error(f"签到任务执行失败: {e}")
        import traceback
        logger_adapter.error(f"详细错误信息: {traceback.format_exc()}")
        screenshot_path = None
        if driver is not None:
            screenshot_path = save_screenshot(driver, current_user, status="failure")
        return {
            'status': False,
            'msg': f'执行异常: {str(e)[:100]}',
            'integral': 0,
            'username': masked_user,
            'screenshot': screenshot_path
        }
    finally:
        if driver is not None:
            try:
                logger_adapter.info("正在关闭 WebDriver...")
                driver.quit()
            except Exception as e:
                logger_adapter.debug(f"关闭 WebDriver 时出错: {e}")

            time.sleep(1)

            try:
                if hasattr(driver, 'service') and driver.service.process:
                    process = driver.service.process
                    pid = process.pid
                    if os.name == 'posix' and pid:
                        subprocess.run(['pkill', '-9', '-P', str(pid)],
                                     stderr=subprocess.DEVNULL)
            except:
                pass

        try:
            unload_selenium_modules()
        except:
            pass


def run_all_accounts():
    """执行所有账号的签到任务"""
    import concurrent.futures

    max_retries = int(os.getenv("CHECKIN_MAX_RETRIES", "2"))
    max_workers = int(os.getenv("MAX_WORKERS", "3"))
    stagger_delay = int(os.getenv("MAX_DELAY", "15"))

    accounts = parse_accounts()
    results = {}

    for i, (username, password) in enumerate(accounts):
        results[username] = {
            'password': password,
            'result': None,
            'retry_count': 0,
            'index': i + 1
        }

    pending_accounts = list(accounts)
    current_attempt = 0

    while pending_accounts and current_attempt <= max_retries:
        if current_attempt == 0:
            logger.info(f"========== 开始执行签到任务（共 {len(pending_accounts)} 个账号，并发数: {max_workers}） ==========")
        else:
            logger.info(f"========== 第 {current_attempt} 次重试（共 {len(pending_accounts)} 个失败账号） ==========")

        failed_accounts = []
        future_to_account = {}

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            for i, (username, password) in enumerate(pending_accounts):
                if i > 0 and stagger_delay > 0:
                    actual_delay = random.randint(5, max(5, stagger_delay))
                    logger.info(f"随机等待 {actual_delay} 秒后启动下一个账号任务...")
                    time.sleep(actual_delay)

                account_idx = results[username]['index']
                retry_info = f"（第 {results[username]['retry_count'] + 1} 次尝试）" if results[username]['retry_count'] > 0 else ""
                logger.info(f"========== 启动账号 {account_idx}/{len(accounts)} {retry_info} ==========")

                future = executor.submit(run_checkin, username, password)
                future_to_account[future] = username

            for future in concurrent.futures.as_completed(future_to_account):
                username = future_to_account[future]
                account_idx = results[username]['index']

                try:
                    result = future.result()
                    results[username]['result'] = result

                    if result['status']:
                        logger.info(f"✅ 账号 {account_idx} 签到成功")
                    else:
                        logger.error(f"❌ 账号 {account_idx} 签到失败: {result['msg']}")
                        results[username]['retry_count'] += 1
                        if results[username]['retry_count'] <= max_retries:
                            failed_accounts.append((username, results[username]['password']))

                except Exception as e:
                    logger.error(f"❌ 账号 {account_idx} 执行异常: {e}")
                    results[username]['retry_count'] += 1
                    if results[username]['retry_count'] <= max_retries:
                        failed_accounts.append((username, results[username]['password']))

        pending_accounts = failed_accounts
        current_attempt += 1

    all_results = [v['result'] for v in results.values() if v['result']]
    success_count = sum(1 for r in all_results if r['status'])
    fail_count = sum(1 for r in all_results if not r['status'])

    logger.info(f"========== 签到任务完成 ==========")
    logger.info(f"总计: {len(all_results)} 个账号，成功: {success_count}，失败: {fail_count}")

    if all_results:
        notification_manager = setup_notifications()
        screenshot_mode = os.getenv("SCREENSHOT_MODE", "failed_only")

        context = {
            'html_full': generate_html_report(all_results, screenshot_mode),
            'html_lite': generate_html_report(all_results, 'none'),
            'summary_html': generate_summary_report(all_results, 'html'),
            'markdown_full': generate_markdown_report(all_results),
            'markdown_lite': generate_markdown_report(all_results, compact=True),
            'summary_markdown': generate_summary_report(all_results, 'markdown'),
        }

        title = f"ChmlFrp 签到报告（{success_count}成功/{fail_count}失败）"
        notification_manager.send_all(title, context)

    cleanup_old_screenshots("temp/screenshots", days=7)

    return success_count == len(all_results)


def setup_notifications():
    manager = NotificationManager()

    pushplus_token = os.getenv("PUSHPLUS_TOKEN", "").strip()
    if pushplus_token:
        manager.add_provider(PushPlusProvider(pushplus_token))

    wxpusher_token = os.getenv("WXPUSHER_APP_TOKEN", "").strip()
    if wxpusher_token:
        uids = os.getenv("WXPUSHER_UIDS", "").strip()
        topic_ids = os.getenv("WXPUSHER_TOPIC_IDS", "").strip()
        manager.add_provider(WXPusherProvider(wxpusher_token, uids, topic_ids))

    dingtalk_token = os.getenv("DINGTALK_ACCESS_TOKEN", "").strip()
    if dingtalk_token:
        secret = os.getenv("DINGTALK_SECRET", "").strip()
        manager.add_provider(DingTalkProvider(dingtalk_token, secret))

    smtp_host = os.getenv("SMTP_HOST", "").strip()
    smtp_user = os.getenv("SMTP_USER", "").strip()
    smtp_pass = os.getenv("SMTP_PASS", "").strip()
    if smtp_host and smtp_user and smtp_pass:
        smtp_port = os.getenv("SMTP_PORT", "465")
        smtp_to = os.getenv("SMTP_TO", "").strip() or smtp_user
        manager.add_provider(EmailProvider(smtp_host, smtp_port, smtp_user, smtp_pass, smtp_to))

    return manager


def scheduled_checkin():
    """定时任务包装器"""
    logger.info(f"定时任务触发 - {now_local().strftime('%Y-%m-%d %H:%M:%S')}")
    success = run_all_accounts()

    if success:
        logger.info("定时签到任务执行成功！")
    else:
        logger.error("定时签到任务执行失败！")

    time.sleep(1)

    schedule_time = os.getenv("SCHEDULE_TIME", "08:00")
    current_time = now_local()
    next_run = current_time.replace(
        hour=int(schedule_time.split(':')[0]),
        minute=int(schedule_time.split(':')[1]),
        second=0,
        microsecond=0
    )

    if next_run <= current_time:
        next_run += timedelta(days=1)

    logger.info(f"✅ 程序继续运行，下次执行时间: {next_run.strftime('%Y-%m-%d %H:%M:%S')}")
    time_diff = next_run - current_time
    hours, remainder = divmod(time_diff.total_seconds(), 3600)
    minutes, _ = divmod(remainder, 60)
    logger.info(f"距离下次执行还有: {int(hours)}小时{int(minutes)}分钟")

    return success


if __name__ == "__main__":
    timeout = int(os.getenv("TIMEOUT", "30000")) // 1000
    max_delay = int(os.getenv("MAX_DELAY", "5"))
    debug = os.getenv("DEBUG", "false").lower() == "true"

    user = os.getenv("CHMLFRP_USERNAME", "username").split("|")[0]
    pwd = os.getenv("CHMLFRP_PASSWORD", "password").split("|")[0]

    run_mode = os.getenv("RUN_MODE", "schedule")
    schedule_time = os.getenv("SCHEDULE_TIME", "08:00")

    logger = setup_logging()
    ver = "1.0.0"
    logger.info("===================================================================")
    logger.info(f"🌧️ ChmlFrp-Qiandao v{ver} (Selenium)")
    logger.info("🔗 基于 Rainyun-Qiandao 项目改造")
    logger.info("===================================================================")
    print("")
    logger.info("已启用日志轮转功能，将自动清理7天前的日志")
    if debug:
        logger.info(f"当前配置: MAX_DELAY={max_delay}秒, TIMEOUT={timeout}秒")

    cleanup_logs_on_startup()
    setup_sigchld_handler()
    cleanup_zombie_processes()

    if run_mode == "schedule":
        logger.info(f"启动定时模式，每天 {schedule_time} 自动执行签到")
        logger.info("程序将持续运行，按 Ctrl+C 退出")

        schedule.every().day.at(schedule_time).do(scheduled_checkin)

        tomorrow_schedule = now_local().replace(hour=int(schedule_time.split(':')[0]),
                                               minute=int(schedule_time.split(':')[1]),
                                               second=0, microsecond=0)
        if tomorrow_schedule <= now_local():
            tomorrow_schedule += timedelta(days=1)
        logger.info(f"每日执行时间: {tomorrow_schedule.strftime('%Y-%m-%d %H:%M:%S')}")

        logger.info("首次启动，将在1分钟后执行首次签到任务")
        first_run_time = now_local() + timedelta(minutes=1)

        logger.info("调度器已启动，等待执行任务...")
        first_run_done = False

        try:
            while True:
                current_time = now_local()

                if not first_run_done and current_time >= first_run_time:
                    logger.info("执行首次签到任务（所有账号）")
                    success = run_all_accounts()
                    if success:
                        logger.info("首次签到任务执行成功！")
                    else:
                        logger.error("首次签到任务执行失败！")
                    first_run_done = True

                schedule.run_pending()
                time.sleep(30)

        except KeyboardInterrupt:
            logger.info("程序已停止")
    else:
        logger.info("运行模式: 单次执行（所有账号）")
        success = run_all_accounts()
        if success:
            logger.info("签到任务执行成功！")
        else:
            logger.error("签到任务执行失败！")
            sys.exit(1)
