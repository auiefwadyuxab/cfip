import os
import re
import sys
import json
import time
import base64
import random
import string
import math
import urllib3
import requests
import threading
import ipaddress
import concurrent.futures
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, unquote
from collections import Counter, defaultdict

# 忽略安全套接字SSL警告，保持控制台整洁
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 线程锁，防止多线程在控制台或输出日志时产生重叠乱序
print_lock = threading.Lock()

# ==================== 1. 路径定义 (自适应GitHub根目录输出) ====================
script_dir = os.path.dirname(os.path.abspath(__file__)) if __file__ else "."
file_all = os.path.join(script_dir, "juhe.txt")
file_test = os.path.join(script_dir, "test.txt")
file_log = os.path.join(script_dir, "log.txt")

# ==================== 2. 静态订阅源定义 ====================
ALL_SOURCES = [
    "https://randomip.pages.dev/?c=all&n=100&p=random",
    "https://bestcf.pages.dev/tiancheng/all.txt",
    "https://bestcf.pages.dev/wetest/ipv4.txt",
    "https://bestcf.pages.dev/uouin/all.txt",
    "https://bestcf.pages.dev/cmliu/all.txt",
    "https://bestcf.pages.dev/cmliu2/all.txt",
    "https://bestcf.pages.dev/xinyitang3/ipv4.txt",
    "https://bestcf.pages.dev/moistr/all.txt",
    "https://bestcf.pages.dev/luoli/all.txt",
    "https://bestcf.pages.dev/kristi/all.txt",
    "https://bestcf.pages.dev/lajiao/all.txt",
    "https://bestcf.pages.dev/cfyes/ipv4.txt",
    "https://bestcf.pages.dev/cfyes/ipv6.txt",
    "https://bestcf.pages.dev/gslege/Cfxyz.txt",
    "https://bestcf.pages.dev/gslege/SG.txt",
    "https://bestcf.pages.dev/gslege/DE.txt",
    "https://bestcf.pages.dev/gslege/US.txt",
    "https://cf.junzhen.qzz.io/best_ips.txt",
    "https://cf.junzhen.qzz.io/best_ips_bj.txt",
    "https://raw.githubusercontent.com/svip-s/cloudflare_ip/refs/heads/main/best_ips.txt",
    "https://raw.githubusercontent.com/love-ztm/cfip/refs/heads/main/best_ips.txt",
    "https://raw.githubusercontent.com/love-ztm/cfip/refs/heads/main/ubest_ips.txt",
    "https://addressesapi.090227.xyz/ip.164746.xyz",
    "https://bestcf.pages.dev/vvhan/ipv4.txt",
    "https://bestcf.pages.dev/vvhan/ipv6.txt",
    "https://bestcf.pages.dev/nirevil/ipv4.txt",
    "https://bestcf.pages.dev/nirevil/ipv6.txt",
    "https://raw.githubusercontent.com/ymyuuu/IPDB/refs/heads/main/BestCF/bestcfv4.txt",
    "https://raw.githubusercontent.com/ymyuuu/IPDB/refs/heads/main/BestCF/bestcfv6.txt",
    "https://raw.githubusercontent.com/joname1/BestCFip/refs/heads/main/ipv4.txt",
    "https://raw.githubusercontent.com/joname1/BestCFip/refs/heads/main/ipv6.txt",
    "https://raw.githubusercontent.com/Senflare/Senflare-IP/refs/heads/main/IPlist-Pro.txt",
    "https://bestcf.pages.dev/ircf/ipv4.txt",
    "https://raw.githubusercontent.com/einsitang/my-fast-cf-ip/refs/heads/master/fastips.txt",
    "https://raw.githubusercontent.com/hubbylei/bestcf/refs/heads/main/bestcf.txt",
    "https://raw.githubusercontent.com/gshtwy/CF-DNS-Clone/refs/heads/main/wetest-cloudflare-v4.txt",
    "https://bestcf.pages.dev/entryip/50.txt",
    "https://bestcf.pages.dev/s5gy/all.txt",
    "https://raw.githubusercontent.com/yuanxiawan/cfipv4db/refs/heads/main/cfip.txt",
    "https://bestcf.pages.dev/lzj/all.txt",
    "https://warp-masque-bestip.pages.dev/?ips=50&level=p0&port=random",
    "https://090227.pages.dev/bestcf?isp=all&ips=50",
    "https://bestcf.pages.dev/domain/all.txt",
    "https://bestcf.pages.dev/domain/Domain-Asia.txt",
    "https://bestcf.pages.dev/vps789/top20.txt",
    "https://bestcf.pages.dev/vps789/top20-8443.txt",
    "https://bestcf.pages.dev/domain/Domain-AI-VPS789.txt",
    "https://bestcf.pages.dev/domain/ygkkk/all.txt",
    "https://bestcf.pages.dev/domain/qms/all.txt",
    "https://bestcf.pages.dev/domain/fiatnorm/all.txt",
    "https://bestcf.pages.dev/domain/senflare/all.txt",
    "https://bestcf.pages.dev/domain/wuya/all.txt",
    "https://bestcf.pages.dev/domain/ircf/all.txt",
    "https://bestcf.pages.dev/domain/Domain-TOP.txt"
]

# 从 GitHub 环境变量中提取 Wild 聚合订阅链接
WILD_SUB_URL = os.environ.get("WILD_SUB_URL", "").strip()

WILD_SUB_HOST = ""
if WILD_SUB_URL:
    try:
        WILD_SUB_HOST = urlparse(WILD_SUB_URL).netloc
    except Exception as e:
        print(f"⚠️ 自动解析 Host 异常: {e}")

# ==================== 3. 强制置顶及泛域名模块 ====================
STATIC_DOMAINS = [
    "you294jwis2.cf.090227.xyz",
    "www.visa.cn",
    "mfa.gov.ua",
    "www.shopify.com",
    "store.ubi.com",
    "staticdelivery.nexusmods.com",
    "time.is",
    "icook.hk",
    "icook.tw",
    "bestcf.030101.xyz",
    "cdn.2020111.xyz",
    "cdns.doon.eu.org",
    "cf.0sm.com",
    "cf.877771.xyz",
    "cf.877774.xyz",
    "cf.900501.xyz",
    "cfip.1323123.xyz",
    "cfip.cfcdn.vip",
    "cfip.xxxxxxxx.tk",
    "cloudflare-dl.byoip.top",
    "cloudflare-ip.mofashi.ltd",
    "fn.130519.xyz",
    "freeyx.cloudflare88.eu.org",
    "nrt.xxxxxxxx.nyc.mn",
    "nrtcfdns.zone.id",
    "saas.sin.fan",
    "xn--b6gac.eu.org",
    "777.ai7777777.xyz"
]

WILDCARD_DOMAINS = [
    "*.cf.090227.xyz",
    "*.tencentapp.cn",
    "*.cloudflare.182682.xyz"
]

# 精准过滤无效的主机占位符
exact_block_hosts = {"127.0.0.1", "localhost", "0.0.0.0", "::1", "::"}
if WILD_SUB_HOST:
    exact_block_hosts.add(WILD_SUB_HOST)

# 增强了垃圾/虚假/广告占位符关键字拦截库
substring_block_keywords = [
    "too many requests", "error", "bestcf.pages.dev", "exception",
    "updated.at", "telegram", "join.my", "channel", "cmliussss.to",
    "github", "unlock.more"
]

# ==================== 4. 编译正则表达式 ====================
ipv4_regex = re.compile(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$')
html_tag_regex = re.compile(r'<[^>]+>')

# ==================== 5. 核心辅助清洗与解析模块 ====================
def clean_comment_and_spaces(text_line):
    """彻底过滤并删除行内的 # 符号及其右边备注"""
    if '#' in text_line:
        text_line = text_line.split('#')[0]
    return text_line.strip()

def generate_random_prefix(length=10):
    """随机生成包含数字和小写字母的前缀以替代泛域名通配符"""
    chars = string.ascii_lowercase + string.digits
    return ''.join(random.choices(chars, k=length))

def calculate_entropy(s):
    """计算字符串的信息熵，用于评估域名的随机无规律程度"""
    if not s:
        return 0
    entropy = 0
    for x in range(256):
        p_x = float(s.count(chr(x))) / len(s)
        if p_x > 0:
            entropy += - p_x * math.log(p_x, 2)
    return entropy

def extract_pure_ip_and_port(ip_port):
    """提取主机名与对应端口，严格对 IPv6 进行去中括号处理"""
    ip_port = ip_port.strip()
    ip_port = re.sub(r'\s+', ' ', ip_port)

    # 1. 已经是空格分隔的裸格式
    if ' ' in ip_port:
        parts = ip_port.rsplit(' ', 1)
        if parts[1].isdigit():
            return parts[0].replace('[', '').replace(']', '').strip(), parts[1].strip()
        return ip_port.replace('[', '').replace(']', '').strip(), "443"

    # 2. 中括号 IPv6
    if ip_port.startswith('['):
        if ']' in ip_port:
            parts = ip_port.split(']')
            host = parts[0][1:].strip()
            remaining = parts[1].strip()
            port = remaining[1:].strip() if remaining.startswith(':') else "443"
            return host, port or "443"
        return ip_port.replace('[', '').replace(']', '').strip(), "443"

    # 3. 原始裸 IPv6 带多冒号无中括号
    if ip_port.count(':') > 1:
        parts = ip_port.rsplit(':', 1)
        if parts[1].isdigit() and 1 <= int(parts[1]) <= 65535:
            return parts[0].strip(), parts[1].strip()
        return ip_port.strip(), "443"

    # 4. 单冒号标准格式
    if ':' in ip_port:
        parts = ip_port.rsplit(':', 1)
        return parts[0].strip(), parts[1].strip()

    return ip_port, "443"

def is_ip(address):
    """验证是否属于标准 IP 结构 (IPv4 或 IPv6)"""
    if ipv4_regex.match(address):
        return True
    if ':' in address:
        return True
    return False

def is_valid_domain(hostname):
    """审查域名格式的合法性"""
    if not hostname or len(hostname) > 255:
        return False
    if hostname.replace(".", "").isdigit():
        return False
    if ".." in hostname or hostname.startswith(".") or hostname.endswith("."):
        return False
    # 彻底拦截一些子域名层级过多（如超过5层）的广告域名推广
    if hostname.count('.') > 5:
        return False
    return bool(re.match(r'^[a-zA-Z0-9][-a-zA-Z0-9.]*[a-zA-Z0-9]$', hostname))

def parse_sharing_link(line):
    """解码和清洗 Vmess/Vless/Trojan/SS 分享链接，提取主机名/端口"""
    line = line.strip()
    if not line:
        return None

    if line.startswith("vmess://"):
        try:
            b64_part = line[8:].strip()
            b64_part += "=" * ((4 - len(b64_part) % 4) % 4)
            decoded = base64.b64decode(b64_part).decode('utf-8', errors='ignore')
            js = json.loads(decoded)
            add = js.get("add") or js.get("host")
            port = js.get("port") or "443"
            if add:
                return f"{add}:{port}"
        except Exception:
            pass
        return None

    if "://" in line:
        try:
            main_part = line.split("://", 1)[1]
            if '#' in main_part:
                main_part = main_part.split('#', 1)[0]
            if '?' in main_part:
                main_part = main_part.split('?', 1)[0]
            if '@' in main_part:
                main_part = main_part.split('@', -1)[-1]
            return main_part
        except Exception:
            pass
        return None

    return line

def robust_decode_base64(raw_content):
    """Base64 自愈解码器"""
    raw_content_stripped = raw_content.strip()
    if not raw_content_stripped:
        return ""

    if "vless://" in raw_content_stripped or "vmess://" in raw_content_stripped:
        return raw_content_stripped

    cleaned_base64 = re.sub(r'\s+', '', raw_content_stripped)
    try:
        missing_padding = len(cleaned_base64) % 4
        if missing_padding:
            cleaned_base64 += "=" * (4 - missing_padding)
        decoded = base64.b64decode(cleaned_base64).decode("utf-8", errors="ignore")
        if "://" in decoded:
            return decoded
    except Exception:
        pass

    decoded_lines = []
    has_decoded_any = False
    for line in raw_content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            line_clean = re.sub(r'\s+', '', line)
            if len(line_clean) > 10 and re.match(r'^[A-Za-z0-9+/=]+$', line_clean):
                missing_padding = len(line_clean) % 4
                if missing_padding:
                    line_clean += "=" * (4 - missing_padding)
                dec_line = base64.b64decode(line_clean).decode("utf-8", errors="ignore")
                if "://" in dec_line:
                    decoded_lines.append(dec_line)
                    has_decoded_any = True
                    continue
        except Exception:
            pass
        decoded_lines.append(line)

    if has_decoded_any:
        return "\n".join(decoded_lines)

    return raw_content

def parse_content_adaptively(text):
    """自适应内容解析器"""
    extracted_lines = []
    if not text:
        return extracted_lines

    if "<" in text and ">" in text:
        text = html_tag_regex.sub('\n', text)

    text = robust_decode_base64(text).strip()

    if text.startswith('{') or text.startswith('['):
        try:
            data = json.loads(text)

            def recurse_json(obj):
                if isinstance(obj, list):
                    for item in obj:
                        recurse_json(item)
                elif isinstance(obj, dict):
                    ip = obj.get('ip') or obj.get('host') or obj.get('node') or obj.get('add')
                    port = obj.get('port') or "443"
                    if ip:
                        extracted_lines.append(f"{ip}:{port}")
                    for v in obj.values():
                        if isinstance(v, (dict, list)):
                            recurse_json(v)

            recurse_json(data)
            return extracted_lines
        except Exception:
            pass

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('//'):
            continue
        parsed_node = parse_sharing_link(line)
        if parsed_node:
            cleaned_line = clean_comment_and_spaces(parsed_node)
            if cleaned_line:
                extracted_lines.append(cleaned_line)

    return extracted_lines

def fetch_single_url_raw(session, url, custom_host=None, is_wild_source=False):
    """拉取原始响应内容与格式（提供给 Wild 统计模块使用），保障大聚合不丢包"""
    headers = {
        'User-Agent': 'v2rayNG/2.2.6',
        'Connection': 'close',
        'Accept-Encoding': 'gzip'
    }
    if custom_host:
        headers['Host'] = custom_host

    timeout_param = (15.0, 90.0) if is_wild_source else (2.0, 5.0)

    try:
        response = session.get(
            url,
            headers=headers,
            timeout=timeout_param,
            verify=False
        )
        if response.status_code == 200:
            content_type = response.headers.get('Content-Type', '').lower()
            text_preview = response.text.strip()

            # CF 阻断挑战页屏蔽
            if 'text/html' in content_type or text_preview.startswith('<!doctype html') or text_preview.startswith('<html'):
                with print_lock:
                    print(f"⚠️ [拦截] 源触发了 Cloudflare 质询，已自动丢弃 HTML 包体。")
                return ""
            return response.text
        else:
            with print_lock:
                print(f"[响应状态异常 {response.status_code}] -> {urlparse(url).netloc}")
    except Exception as e:
        with print_lock:
            print(f"[快速阻断/离线] -> {urlparse(url).netloc} ({type(e).__name__})")
    return ""

def fetch_single_url(session, url, custom_host=None, is_wild_source=False):
    """网络请求主引擎，带有大聚合超时防护"""
    parsed_url = urlparse(url)
    filename = parsed_url.path.split('/')[-1] or parsed_url.netloc
    raw_text = fetch_single_url_raw(session, url, custom_host, is_wild_source)
    if raw_text:
        local_lines = parse_content_adaptively(raw_text)
        with print_lock:
            print(f"[下载成功] -> {filename} (洗净出 {len(local_lines)} 条)")
        return url, local_lines
    return url, []

# ==================== 6. 主程序与双重日志调度引擎 ====================
def main():
    start_time = time.time()
    
    # 建立北京时间 (UTC+8)
    tz_beijing = timezone(timedelta(hours=8))
    bj_now = datetime.now(timezone.utc).astimezone(tz_beijing)
    time_str = bj_now.strftime('%Y-%m-%d %H:%M:%S')

    raw_results = []
    session = requests.Session()

    # 13个源提供商分布追踪字典 (完美精准检测)
    source_mapping = {
        "CM": ["sub.cmliussss.net", "[cm]", "cmliussss"],
        "Mia": ["sub.mia.xx.kg", "[mia]", "@miachatchannel"],
        "天诚1": ["vl.cm.soso.edu.kg", "[天诚1]", "天诚", "zyssorg", "zyssadmin", "ssoadmin", "cloudflareorg"],
        "Moist_R": ["owo.o00o.ooo", "[moist_r]", "[moist r]", "moist_r", "moist r", "mianfeicf"],
        "洛璃": ["loli.sub.us.ci", "洛璃", "[洛璃]"],
        "辣子鸡": ["sub.lzjbaby.com", "辣子鸡", "[辣子鸡]", "lzjjjjjjjjjjj"],
        "辣椒炒肉少放辣": ["sub.xdu.qzz.io", "辣椒炒肉", "buddygator", "gator_ovo"],
        "文烨": ["sub.keaeye.icu", "文烨", "[文烨]", "keaeye"],
        "S5公益": ["sub.995677.xyz", "s5gydl", "s5公益", "[s5公益]"],
        "Kristi": ["sub.mot.cloudns.biz", "kristi", "[kristi]", "marisa", "marisa_kristi"],
        "周润发": ["zrf.zrf.me", "周润发", "[周润发]", "lsmoo"],
        "IDK": ["sub.pjq.cc.cd", "idk", "[idk]", "dbs.com", "cortera.com", "shopify.com", "cmin2", "pjq.cc.cd"],
        "DanFeng": ["sub.danfeng.eu.org", "danfeng", "[danfeng]"]
    }
    
    wild_raw_counter = Counter()
    wild_unique_counter = Counter()

    print(f"==========================================")
    print(f"📡 极速无缝并轨聚合去重开启 | 启动时间: {time_str}")
    print(f"==========================================")

    wild_results_cache = []
    if WILD_SUB_URL:
        print(f"📡 [第一阶段] 正在模拟 v2rayNG 客户端连接并请求 Wild 订阅节点...")
        raw_wild_text = fetch_single_url_raw(session, WILD_SUB_URL, WILD_SUB_HOST, is_wild_source=True)
        
        if raw_wild_text:
            print("✅ 订阅源数据已成功拉取，进入高精确原始比对清洗阶段...")
            decoded_wild_content = robust_decode_base64(raw_wild_text)
            
            # 直接在 Base64 解码后且未破坏节点原本特征(协议、备注、参数)的原始行上进行精准分类统计
            wild_lines = decoded_wild_content.splitlines()
            temp_seen = set()
            
            for line in wild_lines:
                line = line.strip()
                if not line:
                    continue
                
                # 分流源检测
                detected_source = "未知常规优选源"
                line_decoded = unquote(line).lower()
                for source_name, keywords in source_mapping.items():
                    if any(kw in line_decoded for kw in keywords):
                        detected_source = source_name
                        break
                
                wild_raw_counter[detected_source] += 1
                
                # 执行深度清洗
                parsed_node = parse_sharing_link(line)
                if parsed_node:
                    cleaned_line = clean_comment_and_spaces(parsed_node)
                    if cleaned_line:
                        host, port = extract_pure_ip_and_port(cleaned_line)
                        if host:
                            # 规范化 IP
                            if is_ip(host):
                                try:
                                    host = ipaddress.ip_address(host).compressed
                                except ValueError:
                                    pass
                            else:
                                host = host.lower()
                                
                            standard_format = f"{host} {port}"
                            
                            # 判定拦截广告及占位符
                            is_blocked = False
                            if host in exact_block_hosts or any(kw in host for kw in substring_block_keywords):
                                is_blocked = True
                            
                            if not is_blocked:
                                wild_results_cache.append(cleaned_line)
                                if standard_format not in temp_seen:
                                    temp_seen.add(standard_format)
                                    wild_unique_counter[detected_source] += 1
            
            print(f"\n" + "="*50)
            print(" 🎉 Wild 订阅提取与各分流源统计报告：")
            print("="*50)
            print(f" 📈 扫描原始节点数: {len(wild_lines)} 个")
            print("-"*50)
            print(" 📶 13 个订阅器节点贡献分布统计 (原始拉取 -> 实际去重保留):")
            all_sources = ["S5公益", "Moist_R", "CM", "Mia", "天诚1", "洛璃", "辣子鸡", "辣椒炒肉少放辣", "文烨", "Kristi", "周润发", "IDK", "DanFeng"]
            for src in all_sources:
                raw_c = wild_raw_counter.get(src, 0)
                uniq_c = wild_unique_counter.get(src, 0)
                print(f"    - {src.ljust(15)} : {str(raw_c).rjust(4)} -> {str(uniq_c).rjust(4)} 个节点")
            
            other_raw = wild_raw_counter.get("未知常规优选源", 0)
            other_uniq = wild_unique_counter.get("未知常规优选源", 0)
            if other_raw > 0:
                print(f"    - {'其他边缘源'.ljust(15)} : {str(other_raw).rjust(4)} -> {str(other_uniq).rjust(4)} 个节点")
            print("="*50 + "\n")
        else:
            print("❌ 警告：未拉取到 Wild 聚合源数据，或拉取到的内容为空。")
        
        print("🕒 缓冲停顿中，保障 API 节点存活稳定性...")
        time.sleep(1.0)
    else:
        print("💡 提示：未检测到 WILD_SUB_URL 密钥。跳过第一阶段拉取。")

    # 【第二阶段】开启极速常规订阅拉取 (ALL_SOURCES)
    max_fetch_workers = len(ALL_SOURCES)
    print(f"🚀 [第二阶段] 解除并发阀门，开启常规订阅极速多线程并轨拉取 (线程数: {max_fetch_workers})...")
    
    adapter = requests.adapters.HTTPAdapter(pool_connections=max_fetch_workers, pool_maxsize=max_fetch_workers * 2)
    session.mount('http://', adapter)
    session.mount('https://', adapter)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_fetch_workers) as executor:
        future_to_url = {executor.submit(fetch_single_url, session, url, None, False): url for url in ALL_SOURCES}
        for future in concurrent.futures.as_completed(future_to_url):
            try:
                _, res_lines = future.result()
                if res_lines:
                    raw_results.extend(res_lines)
            except Exception:
                pass
    session.close()

    # 【第三阶段】归并大去重与泛域名精洗过滤
    print("\n🧐 [第三阶段] 正在将缓存数据深度合并，启动极速高精格式洗净去重...")
    
    total_raw_crawled_count = len(raw_results) + len(wild_results_cache)
    all_raw_combined = raw_results + wild_results_cache

    unique_entries = set()
    all_extracted_candidates = []
    invalid_count = 0
    duplicate_count = 0

    # 第一步：高精提取和 IP 规范化
    for line in all_raw_combined:
        line = clean_comment_and_spaces(line)
        if not line:
            continue

        host, port = extract_pure_ip_and_port(line)
        if not host:
            continue

        host_lower = host.lower()
        if host_lower in exact_block_hosts or any(kw in host_lower for kw in substring_block_keywords):
            invalid_count += 1
            continue

        # 针对合法 IP 严格执行规范压缩并过滤无效格式
        if is_ip(host):
            try:
                # 压缩 IPv6 并清除大小写混杂及非标准表示形式
                host = ipaddress.ip_address(host).compressed
            except ValueError:
                invalid_count += 1
                continue
        else:
            if not is_valid_domain(host):
                invalid_count += 1
                continue
            host = host_lower

        all_extracted_candidates.append((host, port))

    # 第二步：对泛域名使用信息熵进行严格而又高保留的极客过滤算法
    wildcard_suffixes = [wd.replace('*', '') for wd in WILDCARD_DOMAINS]
    
    ip_records = []
    crawled_domains = []
    wildcard_matches = {suffix: [] for suffix in wildcard_suffixes}

    for host, port in all_extracted_candidates:
        if is_ip(host):
            ip_records.append((host, port))
        else:
            # 匹配泛域名
            matched_any_wildcard = False
            for suffix in wildcard_suffixes:
                if host.endswith(suffix):
                    wildcard_matches[suffix].append((host, port))
                    matched_any_wildcard = True
                    break
            
            if not matched_any_wildcard:
                crawled_domains.append((host, port))

    # 信息熵筛选：防止同一泛域名域名泛滥。只保留最能体现随机自动扫描特性的极客节点
    filtered_wildcard_domains = []
    for suffix, domain_list in wildcard_matches.items():
        if not domain_list:
            continue
        if len(domain_list) <= 10:  # 数量较少时直接全部保留
            filtered_wildcard_domains.extend(domain_list)
        else:
            # 按照信息熵值从大到小排序，高熵值优先保留前 15 个，完美满足多样性与防爆
            entropy_list = []
            for d, p in domain_list:
                prefix = d[:-len(suffix)]
                entropy_list.append((calculate_entropy(prefix), (d, p)))
            entropy_list.sort(key=lambda x: x[0], reverse=True)
            filtered_wildcard_domains.extend([item[1] for item in entropy_list[:15]])

    # 第三步：精细去重并完美排列
    final_output_lines = []

    # 1. 强制置顶静态优选域名
    for static_d in STATIC_DOMAINS:
        final_output_lines.append(f"{static_d} 443")

    # 2. 泛域名置顶生成
    for wd in WILDCARD_DOMAINS:
        random_prefix = generate_random_prefix(10)
        formatted_wd = wd.replace('*', random_prefix)
        final_output_lines.append(f"{formatted_wd} 443")

    # 3. 筛选后的高熵值泛域名优选实例
    for d, p in filtered_wildcard_domains:
        final_output_lines.append(f"{d} {p}")

    # 4. 经过标准压缩整理的纯 IP 序列（进行精确去重并升序排列）
    for ip, p in sorted(ip_records):
        final_output_lines.append(f"{ip} {p}")

    # 5. 普通优质爬虫域名
    for d, p in sorted(crawled_domains):
        final_output_lines.append(f"{d} {p}")

    # 第四步：彻底全局去重（保持节点在 final_output_lines 中的出场顺序不变）
    seen_final = set()
    cleaned_output = []
    for line in final_output_lines:
        h, p = extract_pure_ip_and_port(line)
        standard_node = f"{h} {p}"
        if standard_node in seen_final:
            duplicate_count += 1
            continue
        seen_final.add(standard_node)
        cleaned_output.append(standard_node)

    # ==================== 7. 落盘并写入总结日志 ====================
    try:
        # 1. 写入原本的 juhe.txt
        with open(file_all, "w", encoding="utf-8") as f:
            for line in cleaned_output:
                f.write(line + "\n")

        # 2. 写入新增要求的 test.txt (格式严格定义为: host:port#HK, IPv6 节点带方括号)
        with open(file_test, "w", encoding="utf-8") as f_test:
            for line in cleaned_output:
                h, p = extract_pure_ip_and_port(line)
                if ":" in h:  # IPv6
                    f_test.write(f"[{h}]:{p}#HK\n")
                else:
                    f_test.write(f"{h}:{p}#HK\n")

        elapsed = time.time() - start_time

        # 智能自增聚合数
        next_run_num = 1
        if os.path.exists(file_log):
            try:
                with open(file_log, "r", encoding="utf-8") as r_log:
                    log_content = r_log.read()
                    matches = re.findall(r'聚合(\d+)日志', log_content)
                    if matches:
                        next_run_num = max(int(m) for m in matches) + 1
            except Exception:
                pass

        log_header = f"聚合{next_run_num}日志"

        # 分析节点类型分布
        ipv4_count = 0
        ipv6_count = 0
        domain_count = 0
        for node_line in cleaned_output:
            h = node_line.split(' ')[0]
            if is_ip(h):
                if ':' in h:
                    ipv6_count += 1
                else:
                    ipv4_count += 1
            else:
                domain_count += 1

        dashboard = []
        dashboard.append("===========================================")
        dashboard.append(f"[北京时间] {time_str}")
        dashboard.append(f"{log_header}")
        dashboard.append("-------------------------------------------")
        dashboard.append(f" ✨ 极速聚合去重完美收官！(整体耗时: {elapsed:.2f} 秒)")
        dashboard.append(f" 💾 输出文件 1 (空格分隔): juhe.txt")
        dashboard.append(f" 💾 输出文件 2 (冒号加备注): test.txt")
        dashboard.append(f" 💾 日志追加: log.txt")
        dashboard.append("-------------------------------------------")
        dashboard.append(f" 📈 原始拉取节点总数: {total_raw_crawled_count} 个")
        dashboard.append(f" 🚫 过滤无效/脏数据节点: {invalid_count} 个")
        dashboard.append(f" 🔄 除去重复冗余节点: {duplicate_count} 个")
        dashboard.append(f" 💾 最终清洗导出节点量: {len(cleaned_output)} 个")
        dashboard.append("-------------------------------------------")
        dashboard.append(" 📊 导出节点结构解析:")
        dashboard.append(f"    - IPv4 优选节点: {ipv4_count} 个")
        dashboard.append(f"    - IPv6 优选节点: {ipv6_count} 个 (标准非标格式：去中括号 + 标准压缩)")
        dashboard.append(f"    - 域名 优选节点: {domain_count} 个")
        dashboard.append("===========================================\n\n")

        full_dashboard_text = "\n".join(dashboard)
        print(full_dashboard_text)

        with open(file_log, "a", encoding="utf-8") as f_log:
            f_log.write(full_dashboard_text)

    except Exception as e:
        print(f"❌ 落盘保存失败！错误原因: {e}")


if __name__ == "__main__":
    main()