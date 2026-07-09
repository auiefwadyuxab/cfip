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
from urllib.parse import urlparse, unquote
from collections import Counter, defaultdict

# 忽略安全套接字SSL警告，保持控制台整洁
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 线程锁，防止多线程在控制台或输出日志时产生重叠乱序
print_lock = threading.Lock()

# ==================== 1. 路径定义 (自适应同级目录输出) ====================
script_dir = os.path.dirname(os.path.abspath(__file__)) if __file__ else "."
file_all = os.path.join(script_dir, "juhe.txt")
file_log = os.path.join(script_dir, "log.txt")

# ==================== 2. 静态及动态订阅源定义 ====================
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

# 自动从 WILD_SUB_URL 中安全提取 Host，防止写死或由于 URL 变更导致变量维护失效
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


# ==================== 5. 核心辅助清洗与解析模块 ====================
def clean_comment_and_spaces(text_line):
    """彻底过滤并删除行内的 # 符号及其右边备注"""
    if '#' in text_line:
        text_line = text_line.split('#')[0]
    return text_line.strip()


def extract_pure_ip_and_port(ip_port):
    """提取主机名(IP或域名)与对应端口，彻底移除 IPv6 [ ]"""
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
    """验证是否属于标准 IP 结构"""
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
    return bool(re.match(r'^[a-zA-Z0-9][-a-zA-Z0-9.]*[a-zA-Z0-9]$', hostname))


def parse_sharing_link(line):
    """极速解包和解析 Vmess/Vless/Trojan/SS 分享链接"""
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
    """深度自适应内容清洗与转换器"""
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


def fetch_single_url(session, url, custom_host=None, is_wild_source=False):
    """极速单源网络连接，针对 Wild 源进行超长 ReadTimeout 保护以免 Actions 运行中被断开"""
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

    # Wild大聚合源需要 15秒建立连接，90秒数据读取，其余54个常规源则为 2秒连接，5秒阻断
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

            # HTML防火墙或5秒盾页面直接废弃
            if 'text/html' in content_type or text_preview.startswith('<!doctype html') or text_preview.startswith('<html'):
                with print_lock:
                    print(f"⚠️ [拦截] 源 {filename} 触发了 Cloudflare 防火墙盾阻断，已自动丢弃 HTML 数据。")
                return url, []

            response.encoding = 'utf-8'
            local_lines = parse_content_adaptively(response.text)
            with print_lock:
                print(f"[下载成功] -> {filename} (洗净出 {len(local_lines)} 条)")
        else:
            with print_lock:
                print(f"[响应异常 状态码: {response.status_code}] -> {filename}")
    except Exception as e:
        with print_lock:
            print(f"[快速阻断/离线] -> {filename} ({type(e).__name__})")
    return url, local_lines


# ==================== 6. 主逻辑流程控制与统计引擎 ====================
def main():
    start_time = time.time()
    
    # 精确获取北京时间 UTC+8
    tz_beijing = timezone(timedelta(hours=8))
    bj_now = datetime.now(timezone.utc).astimezone(tz_beijing)
    time_str = bj_now.strftime('%Y-%m-%d %H:%M:%S')

    raw_results = []
    session = requests.Session()

    # 13个源提供商逆向检索字典，用于完美恢复 Wild 日志分布追踪
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
    
    # 统计专用的 Counter
    wild_raw_counter = Counter()
    wild_unique_counter = Counter()

    # 泛域名后缀模板（即去掉后面的通配星号）
    wildcard_suffixes = [wd.replace('*', '') for wd in WILDCARD_DOMAINS]

    # 用于动态跟踪泛域名中所有前缀出现频次的 defaultdict
    # 结构：{ ".cf.090227.xyz": { "you294jwis2": 15, "cf": 2 } }
    prefix_frequency_tracker = {suffix: defaultdict(int) for suffix in wildcard_suffixes}

    print(f"==========================================")
    print(f"📡 极速无缝并轨聚合去重启动 | 北京时间: {time_str}")
    print(f"==========================================")

    # 第一阶段：先开始拉取 Wild 聚合源
    wild_results_cache = []
    if WILD_SUB_URL:
        print(f"🔗 [第一阶段] 启动 Wild 订阅聚合源拉取 -> 动态 Host: {WILD_SUB_HOST}...")
        _, fetched_lines = fetch_single_url(session, WILD_SUB_URL, WILD_SUB_HOST, is_wild_source=True)
        
        if fetched_lines:
            wild_results_cache = fetched_lines
            print(f"✅ Wild 订阅拉取数据成功！已安全缓存 {len(wild_results_cache)} 条原始未清洗记录。")
            
            # --- 【重点要求】立即在 Phase 1 之后计算并输出 Wild 对应的专属统计日志 ---
            for line in wild_results_cache:
                detected_source = "未知常规优选源"
                line_decoded = unquote(line).lower()
                for source_name, keywords in source_mapping.items():
                    if any(kw in line_decoded for kw in keywords):
                        detected_source = source_name
                        break
                wild_raw_counter[detected_source] += 1
                
                # 预先做一下虚拟清洗以估算去重保留率
                line_clean = clean_comment_and_spaces(line)
                host, port = extract_pure_ip_and_port(line_clean)
                if host and host.lower() not in exact_block_hosts:
                    # 记录泛域名前缀
                    host_lower = host.lower()
                    for suffix in wildcard_suffixes:
                        if host_lower.endswith(suffix):
                            prefix = host_lower[:-len(suffix)]
                            if prefix and prefix != 'cf' and is_valid_domain(prefix):
                                prefix_frequency_tracker[suffix][prefix] += 1

                    # 模拟保留量加一
                    wild_unique_counter[detected_source] += 1

            # 立即在终端渲染 Wild 专属面板
            print("\n" + "-"*50)
            print(" 📶 [第一阶段统计] 13 个主流重度订阅器原始贡献统计:")
            all_sources = ["S5公益", "Moist_R", "CM", "Mia", "天诚1", "洛璃", "辣子鸡", "辣椒炒肉少放辣", "文烨", "Kristi", "周润发", "IDK", "DanFeng"]
            for src in all_sources:
                raw_c = wild_raw_counter.get(src, 0)
                # 估计去重值
                uniq_c = wild_unique_counter.get(src, 0)
                print(f"    - {src.ljust(15)} : {str(raw_c).rjust(4)} -> {str(uniq_c).rjust(4)} 个节点")
            print("-"*50)
        else:
            print("❌ 警告：未拉取到 Wild 聚合源数据，或拉取到的内容为空。")
        
        # 机器平滑缓冲停顿 1.0 秒，抗高并发限制
        print("🕒 执行机器安全缓冲延迟停顿...")
        time.sleep(1.0)
    else:
        print("💡 提示：未检测到 WILD_SUB_URL 密钥环境变量。跳过第一阶段 Wild 聚合源拉取。")

    # 第二阶段：极致多线程并轨拉取常规订阅（ALL_SOURCES）
    max_fetch_workers = len(ALL_SOURCES)
    print(f"\n🚀 [第二阶段] 开启常规订阅极速多线程并轨拉取 (并发线程数: {max_fetch_workers})...")
    
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

    # 第三阶段：进行数据深度汇总合并，统计泛域名前缀频次，进行重构式去重
    print("\n🧐 [第三阶段] 数据全部拉取完成！正在合并两个脚本全部节点并执行深度重构去重...")
    
    all_raw_combined = raw_results + wild_results_cache
    total_raw_crawled_count = len(all_raw_combined)

    # 1. 第一轮扫描：统计所有合并节点中，匹配泛域名后缀的所有前缀的出现频次
    for line in all_raw_combined:
        line_clean = clean_comment_and_spaces(line)
        if not line_clean:
            continue
        host, port = extract_pure_ip_and_port(line_clean)
        if not host:
            continue
        host_lower = host.lower()
        for suffix in wildcard_suffixes:
            if host_lower.endswith(suffix):
                # 提取前缀
                prefix = host_lower[:-len(suffix)]
                # 排除空前缀、cf本身（它是默认值）以及非法字符，进行高精度累加
                if prefix and prefix != 'cf' and is_valid_domain(prefix):
                    prefix_frequency_tracker[suffix][prefix] += 1
            elif host_lower == suffix[1:]: # 例如，裸域名 cf.090227.xyz
                prefix_frequency_tracker[suffix]['cf'] += 1

    # 2. 计算每个泛域名后缀的“最高频前缀（皇冠赢家）”
    suffix_to_best_prefix = {}
    print("👑 [前缀统计中] 开始评估最强泛域名前缀 (最热频次选作前缀，相等或无则使用 cf):")
    for suffix in wildcard_suffixes:
        counts = prefix_frequency_tracker[suffix]
        if not counts:
            suffix_to_best_prefix[suffix] = "cf"
            print(f"    - 后缀 {suffix.ljust(25)} : 无提取到任何可用前缀，默认使用前缀 [cf]")
        else:
            # 找到最高频次
            max_frequency = max(counts.values())
            # 筛选出所有达到最高频次的前缀
            winners = [k for k, v in counts.items() if v == max_frequency]
            if len(winners) == 1:
                suffix_to_best_prefix[suffix] = winners[0]
                print(f"    - 后缀 {suffix.ljust(25)} : 赢家为前缀 [{winners[0]}] (出现频数: {max_frequency})")
            else:
                # 若并列最多，则默认仍采用 cf
                suffix_to_best_prefix[suffix] = "cf"
                print(f"    - 后缀 {suffix.ljust(25)} : 前缀并列出现 (包含{winners})，默认退守至 [cf]")

    # 3. 第二轮扫描：将匹配后缀的所有节点，重置为主干皇冠赢家的前缀，然后执行精确去重
    unique_entries = set()
    invalid_count = 0
    duplicate_count = 0

    ip_records = []
    crawled_domains = []

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

        # 泛域名重构：若检测到其后缀匹配模板，强制将其前缀修改重写为 winner 前缀
        for suffix in wildcard_suffixes:
            if host_lower.endswith(suffix) or host_lower == suffix[1:]:
                # 寻找它的最高频前缀，将其强行重写为: {winner_prefix}{suffix}
                best_prefix = suffix_to_best_prefix.get(suffix, "cf")
                host = f"{best_prefix}{suffix}"
                break

        # 生成标准的非标优选格式进行严格去重
        standard_format = f"{host} {port}"
        if standard_format in unique_entries:
            duplicate_count += 1
            continue

        unique_entries.add(standard_format)

        # 二次安全洗涤并分类存放
        if is_ip(host):
            ip_records.append((host, port))
        else:
            if not is_valid_domain(host):
                invalid_count += 1
                continue
            crawled_domains.append((host, port))

    # ==================== 7. 拼接最终输出排版顺序 ====================
    final_output_lines = []

    # 1. 开头：强制前置特有静态域名
    for static_d in STATIC_DOMAINS:
        final_output_lines.append(f"{static_d} 443")

    # 2. 开头：将固定模板泛域名的 * 替换为选出的最高频赢家前缀
    for wd in WILDCARD_DOMAINS:
        suffix = wd.replace('*', '')
        best_prefix = suffix_to_best_prefix.get(suffix, "cf")
        formatted_wd = f"{best_prefix}{suffix}"
        final_output_lines.append(f"{formatted_wd} 443")

    # 3. 追加去重后的纯 IP
    for ip, p in sorted(ip_records):
        final_output_lines.append(f"{ip} {p}")

    # 4. 追加末尾：剩余所有的普通优选域名
    written_nodes = set()
    cleaned_output = []

    # 将开头已固定的行先放入去重集，防止下方域名产生任何重叠
    for node_line in final_output_lines:
        cleaned_output.append(node_line)
        written_nodes.add(node_line)

    for d, p in sorted(crawled_domains):
        standard_node = f"{d} {p}"
        if standard_node not in written_nodes:
            cleaned_output.append(standard_node)
            written_nodes.add(standard_node)

    # ==================== 8. 保存并在 log.txt 中记录精炼日志 ====================
    try:
        # 1. 物理写入 juhe.txt
        with open(file_all, "w", encoding="utf-8") as f:
            for line in cleaned_output:
                f.write(line + "\n")

        elapsed = time.time() - start_time

        # 2. 智能计算当前是第几次聚合
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

        # 3. 完美整合的仪表盘
        dashboard = []
        dashboard.append("===========================================")
        dashboard.append(f"[北京时间] {time_str}")
        dashboard.append(f"{log_header}")
        dashboard.append("-------------------------------------------")
        dashboard.append(f" ✨ 极速聚合去重完美结束！(整体耗时: {elapsed:.2f} 秒)")
        dashboard.append(f" 💾 输出文件: juhe.txt")
        dashboard.append(f" 💾 追加写入日志文件: log.txt")
        dashboard.append("-------------------------------------------")
        dashboard.append(f" 📈 原始读取总行数/节点数: {total_raw_crawled_count} 个")
        dashboard.append(f" 🚫 过滤无效占位/异常节点数: {invalid_count} 个")
        dashboard.append(f" 🔄 除去重复/冗余重叠节点数: {duplicate_count} 个")
        dashboard.append(f" 💾 最终去重保留节点总量: {len(cleaned_output)} 个")
        dashboard.append("===========================================\n\n")

        full_dashboard_text = "\n".join(dashboard)
        
        # 同时向控制台打印
        print(full_dashboard_text)

        # 追加式写入 log.txt
        with open(file_log, "a", encoding="utf-8") as f_log:
            f_log.write(full_dashboard_text)

    except Exception as e:
        print(f"❌ 落盘保存失败！错误原因: {e}")


if __name__ == "__main__":
    main()