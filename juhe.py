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
import concurrent.futures
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

# 忽略安全套接字SSL警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 线程锁，防止控制台多线程输出错乱
print_lock = threading.Lock()

# ==================== 1. 路径定义 (GitHub Actions 根目录兼容) ====================
script_dir = os.path.dirname(os.path.abspath(__file__)) if __file__ else "."
file_all = os.path.join(script_dir, "juhe.txt")
file_log = os.path.join(script_dir, "log.txt")

# ==================== 2. 静态及动态爬取源 ====================
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

# 从 GitHub Secrets 读取动态配置（去敏感变量化）
WILD_SUB_URL = os.environ.get("WILD_SUB_URL", "").strip()
WILD_SUB_HOST = os.environ.get("WILD_SUB_HOST", "").strip()

# ==================== 3. 强制固定的前置域名 ====================
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

# 屏蔽列表
exact_block_hosts = {"127.0.0.1", "localhost", "0.0.0.0", "::1", "::"}
if WILD_SUB_HOST:
    exact_block_hosts.add(WILD_SUB_HOST)

substring_block_keywords = [
    "too many requests", "error", "bestcf.pages.dev", "exception"
]

# ==================== 4. 编译正则表达式 ====================
ipv4_regex = re.compile(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$')
html_tag_regex = re.compile(r'<[^>]+>')


# ==================== 5. 核心辅助解析算法 ====================
def generate_random_prefix(length=10):
    """生成前置混淆随机字符串"""
    chars = string.ascii_lowercase + string.digits
    return ''.join(random.choices(chars, k=length))


def calculate_entropy(s):
    """利用香农信息熵原理计算文本混乱度"""
    if not s:
        return 0
    entropy = 0
    for x in range(256):
        p_x = float(s.count(chr(x))) / len(s)
        if p_x > 0:
            entropy += - p_x * math.log(p_x, 2)
    return entropy


def clean_comment_and_spaces(text_line):
    """彻底剥离 # 及其后面的描述信息"""
    if '#' in text_line:
        text_line = text_line.split('#')[0]
    return text_line.strip()


def extract_pure_ip_and_port(ip_port):
    """提取标准化 host 和 port 形式，并剥除 IPv6 包含的中括号"""
    ip_port = ip_port.strip()
    ip_port = re.sub(r'\s+', ' ', ip_port)

    # 1. 已经是空格分隔的非标格式
    if ' ' in ip_port:
        parts = ip_port.rsplit(' ', 1)
        if parts[1].isdigit():
            return parts[0].replace('[', '').replace(']', '').strip(), parts[1].strip()
        return ip_port.replace('[', '').replace(']', '').strip(), "443"

    # 2. 带方括号的 IPv6 标准形式
    if ip_port.startswith('['):
        if ']' in ip_port:
            parts = ip_port.split(']')
            host = parts[0][1:].strip()
            remaining = parts[1].strip()
            port = remaining[1:].strip() if remaining.startswith(':') else "443"
            return host, port or "443"
        return ip_port.replace('[', '').replace(']', '').strip(), "443"

    # 3. 未带方括号且包含多重冒号的裸 IPv6 地址
    if ip_port.count(':') > 1:
        parts = ip_port.rsplit(':', 1)
        if parts[1].isdigit() and 1 <= int(parts[1]) <= 65535:
            return parts[0].strip(), parts[1].strip()
        return ip_port.strip(), "443"

    # 4. 含有单冒号的标准格式
    if ':' in ip_port:
        parts = ip_port.rsplit(':', 1)
        return parts[0].strip(), parts[1].strip()

    return ip_port, "443"


def is_ip(address):
    """检测是否属于标准 IP 结构"""
    if ipv4_regex.match(address):
        return True
    if ':' in address:
        return True
    return False


def is_valid_domain(hostname):
    """判定提取值是否是合规的主机域名"""
    if not hostname or len(hostname) > 255:
        return False
    if hostname.replace(".", "").isdigit():
        return False
    if ".." in hostname or hostname.startswith(".") or hostname.endswith("."):
        return False
    return bool(re.match(r'^[a-zA-Z0-9][-a-zA-Z0-9.]*[a-zA-Z0-9]$', hostname))


def parse_sharing_link(line):
    """极客级解析提取订阅分享协议：Vmess/Vless/Trojan/SS"""
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
    """智能Base64自愈解码引擎（逐行加整体解码防断裂）"""
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
    """深层内容自适应清洗解析：HTML脫水，解密，协议反提取"""
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


def fetch_single_url(session, url, custom_host=None):
    """极致单源抓取，配合高阻断超时策略"""
    local_lines = []
    parsed_url = urlparse(url)
    filename = parsed_url.path.split('/')[-1] or parsed_url.netloc

    headers = {
        'User-Agent': 'v2rayNG/2.2.6',
        'Connection': 'close',
        'Accept-Encoding': 'gzip'
    }
    if custom_host:
        headers['Host'] = custom_host

    try:
        response = session.get(
            url,
            headers=headers,
            timeout=(2.0, 5.0),
            verify=False
        )
        if response.status_code == 200:
            content_type = response.headers.get('Content-Type', '').lower()
            text_preview = response.text.strip()

            #CF防火墙和异常HTML过滤机制
            if 'text/html' in content_type or text_preview.startswith('<!doctype html') or text_preview.startswith('<html'):
                with print_lock:
                    print(f"⚠️ [拦截] 源 {filename} 触发了盾质询，已抛弃HTML包体。")
                return url, []

            response.encoding = 'utf-8'
            local_lines = parse_content_adaptively(response.text)
            with print_lock:
                print(f"[下载成功] -> {filename} (解析出 {len(local_lines)} 条)")
        else:
            with print_lock:
                print(f"[响应状态码错误 {response.status_code}] -> {filename}")
    except Exception as e:
        with print_lock:
            print(f"[连接超时/跳过] -> {filename} ({type(e).__name__})")
    return url, local_lines


# ==================== 6. 核心流程编排 ====================
def main():
    start_time = time.time()

    # 动态抓取合并任务源池
    sources_to_crawl = list(ALL_SOURCES)
    has_wild_source = False

    if WILD_SUB_URL:
        sources_to_crawl.append(WILD_SUB_URL)
        has_wild_source = True
        print("🔗 检测到 Github Secrets 注入的动态聚合订阅源，已并入全队列。")
    else:
        print("💡 提示：未检测到 WILD_SUB_URL 密钥环境变量。将只拉取常规订阅。")

    max_fetch_workers = len(sources_to_crawl)
    print(f"🚀 [第一步] 解除并发阀门，开启全通道激进秒级拉取... (并发线程: {max_fetch_workers})")

    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(pool_connections=max_fetch_workers, pool_maxsize=max_fetch_workers * 2)
    session.mount('http://', adapter)
    session.mount('https://', adapter)

    raw_results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_fetch_workers) as executor:
        # 构建 Future 映射
        future_to_url = {}
        for url in sources_to_crawl:
            if url == WILD_SUB_URL and WILD_SUB_HOST:
                # 聚合源使用特殊的 Host
                f = executor.submit(fetch_single_url, session, url, WILD_SUB_HOST)
            else:
                f = executor.submit(fetch_single_url, session, url, None)
            future_to_url[f] = url

        for future in concurrent.futures.as_completed(future_to_url):
            try:
                _, res_lines = future.result()
                if res_lines:
                    raw_results.extend(res_lines)
            except Exception:
                pass
    session.close()

    print(f"\n🧐 [第二步] 抓取完成！开始数据深度过滤、信息熵去重、去噪排序...")

    unique_entries = set()
    for line in raw_results:
        line = clean_comment_and_spaces(line)
        if not line:
            continue

        host, port = extract_pure_ip_and_port(line)
        if not host:
            continue

        host_lower = host.lower()
        # 拦截无效占位符与垃圾脏数据
        if host_lower in exact_block_hosts or any(kw in host_lower for kw in substring_block_keywords):
            continue

        unique_entries.add((host, port))

    # 分析匹配星号泛域名模版
    wildcard_suffixes = [wd.replace('*', '') for wd in WILDCARD_DOMAINS]

    ip_records = []
    crawled_domains = []
    wildcard_matches = {suffix: [] for suffix in wildcard_suffixes}

    for host, port in unique_entries:
        if is_ip(host):
            ip_records.append((host, port))
        else:
            if not is_valid_domain(host):
                continue

            matched_any_wildcard = False
            for suffix in wildcard_suffixes:
                if host.endswith(suffix):
                    wildcard_matches[suffix].append((host, port))
                    matched_any_wildcard = True
                    break

            if not matched_any_wildcard:
                crawled_domains.append((host, port))

    # 信息熵清洗：在泛域名后缀出现多个子域名时，优选无规则（最高混淆性）的实例
    filtered_wildcard_domains = []
    for suffix, domain_list in wildcard_matches.items():
        if not domain_list:
            continue
        if len(domain_list) == 1:
            filtered_wildcard_domains.append(domain_list[0])
        else:
            best_domain_pair = None
            max_entropy = -1.0
            for d, p in domain_list:
                prefix = d[:-len(suffix)]
                entropy_val = calculate_entropy(prefix)
                if entropy_val > max_entropy:
                    max_entropy = entropy_val
                    best_domain_pair = (d, p)
            if best_domain_pair:
                filtered_wildcard_domains.append(best_domain_pair)

    # ==================== 7. 拼装为最纯净严格非标优选格式 ====================
    final_output_lines = []

    # 排布 1: 置顶静态与随机乱序泛域名
    for static_d in STATIC_DOMAINS:
        final_output_lines.append(f"{static_d} 443")

    for wd in WILDCARD_DOMAINS:
        random_prefix = generate_random_prefix(10)
        formatted_wd = wd.replace('*', random_prefix)
        final_output_lines.append(f"{formatted_wd} 443")

    # 排布 2: 最优信息熵筛选出来的泛域名实例
    for d, p in filtered_wildcard_domains:
        final_output_lines.append(f"{d} {p}")

    # 排布 3: 提取的纯 IP
    for ip, p in sorted(ip_records):
        final_output_lines.append(f"{ip} {p}")

    # 排布 4: 剩余普通域名
    for d, p in sorted(crawled_domains):
        final_output_lines.append(f"{d} {p}")

    # ==================== 8. 落盘物理存储并自动写入日志 ====================
    # 统计构成
    ipv4_count = 0
    ipv6_count = 0
    domain_count = 0
    for node_line in final_output_lines:
        h = node_line.split(' ')[0]
        if is_ip(h):
            if ':' in h:
                ipv6_count += 1
            else:
                ipv4_count += 1
        else:
            domain_count += 1

    try:
        with open(file_all, "w", encoding="utf-8") as f:
            for line in final_output_lines:
                f.write(line + "\n")

        elapsed = time.time() - start_time

        # 获取精确的北京时间 (UTC+8)
        tz_beijing = timezone(timedelta(hours=8))
        bj_now = datetime.now(timezone.utc).astimezone(tz_beijing)
        time_str = bj_now.strftime('%Y-%m-%d %H:%M:%S')

        # 智能计算是第几次聚合
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

        # 组织详细仪表盘状态，同时写入终端和 log.txt
        dashboard = (
            f"===========================================\n"
            f"[北京时间] {time_str}\n"
            f"{log_header}\n"
            f"-------------------------------------------\n"
            f" ✨ 极速聚合抓取完美执行结束！(耗时: {elapsed:.2f}s)\n"
            f" 💾 输出非标优选路径: {file_all}\n"
            f" 💾 本次运行日志追加至: {file_log}\n"
            f" 📈 实际生成非标优选节点总量: {len(final_output_lines)} 个\n"
            f" 📊 节点构成统计分析:\n"
            f"    - IPv4 优选节点数: {ipv4_count} 个\n"
            f"    - IPv6 优选节点数: {ipv6_count} 个 (已自动压缩转换)\n"
            f"    - 域名 优选节点数: {domain_count} 个\n"
            f"===========================================\n\n"
        )

        print(dashboard)

        # 追加式写入 log.txt
        with open(file_log, "a", encoding="utf-8") as f_log:
            f_log.write(dashboard)

    except Exception as e:
        print(f"❌ 写入存储文件失败！原因: {e}")


if __name__ == "__main__":
    main()