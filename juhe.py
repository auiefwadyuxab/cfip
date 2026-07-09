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

# ==================== 1. 路径定义 (自适应GitHub根目录输出) ====================
script_dir = os.path.dirname(os.path.abspath(__file__)) if __file__ else "."
file_all = os.path.join(script_dir, "juhe.txt")
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

# 自动从 WILD_SUB_URL 中安全提取 Host，防止由于变更导致脚本失效
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
    """提取主机名与对应端口，彻底移除 IPv6 [ ]"""
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
    """解码和清洗 Vmess/Vless/Trojan/SS 分享链接"""
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


def fetch_single_url(session, url, custom_host=None, is_wild_source=False):
    """网络请求主引擎，带有大聚合超时防护与5秒盾抛弃机制"""
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

    # 针对 Wild 大聚合专门设置的超长读取超时（15s/90s），其余普通源则 2s/5s 极速阻断防止拖延
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
                    print(f"⚠️ [拦截] 源 {filename} 触发了 Cloudflare 防火墙，已自动丢弃 HTML 包体。")
                return url, []

            response.encoding = 'utf-8'
            local_lines = parse_content_adaptively(response.text)
            with print_lock:
                print(f"[下载成功] -> {filename} (洗净出 {len(local_lines)} 条)")
        else:
            with print_lock:
                print(f"[响应状态异常 {response.status_code}] -> {filename}")
    except Exception as e:
        with print_lock:
            print(f"[快速阻断/离线] -> {filename} ({type(e).__name__})")
    return url, local_lines


# ==================== 6. 主程序与双重日志调度引擎 ====================
def main():
    start_time = time.time()
    
    # 建立北京时间 (UTC+8)
    tz_beijing = timezone(timedelta(hours=8))
    bj_now = datetime.now(timezone.utc).astimezone(tz_beijing)
    time_str = bj_now.strftime('%Y-%m-%d %H:%M:%S')

    raw_results = []
    session = requests.Session()

    # 13个源提供商分布追踪字典 (1.wild 原版完美抄录)
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

    # 【第一阶段】执行 Wild 并即时输出专属订阅器日志
    wild_results_cache = []
    if WILD_SUB_URL:
        print(f"📡 [第一阶段] 正在模拟 v2rayNG 客户端连接并请求 Wild 订阅节点...")
        _, fetched_lines = fetch_single_url(session, WILD_SUB_URL, WILD_SUB_HOST, is_wild_source=True)
        
        if fetched_lines:
            wild_results_cache = fetched_lines
            print("✅ 订阅源数据已成功拉取并进入缓存！")
            print("⚡ 开始进行智能 Base64 解码并统计 Wild 订阅器源贡献度...")
            
            # 立即进行逆向映射统计
            temp_seen = set()
            for line in wild_results_cache:
                detected_source = "未知常规优选源"
                line_decoded = unquote(line).lower()
                for source_name, keywords in source_mapping.items():
                    if any(kw in line_decoded for kw in keywords):
                        detected_source = source_name
                        break
                wild_raw_counter[detected_source] += 1
                
                # 评估临时去重后该订阅器的保留数
                line_clean = clean_comment_and_spaces(line)
                host, port = extract_pure_ip_and_port(line_clean)
                if host:
                    standard_format = f"{host.lower()} {port}"
                    if standard_format not in temp_seen:
                        temp_seen.add(standard_format)
                        wild_unique_counter[detected_source] += 1
            
            # 【重要】拉取成功后直接缓存并打印原 wild 脚本的原生日志（不放在最后）
            print("\n" + "="*50)
            print(" 🎉 Wild 订阅提取与各分流源统计报告：")
            print("="*50)
            print(f" 📈 扫描原始节点数: {len(wild_results_cache)} 个")
            print("-"*50)
            print(" 📶 13 个订阅器节点贡献分布统计 (原始拉取 -> 临时去重保留):")
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
        
        # 机器平缓抗压停顿 1.0 秒，抗高频并发阻断
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

    # 【第三阶段】归并大去重，启动泛域名“最热前缀竞争”算法
    print("\n🧐 [第三阶段] 正在将缓存数据深度合并，启动极速自适应去重与泛域名王者竞争重写...")
    
    total_raw_crawled_count = len(raw_results) + len(wild_results_cache)
    all_raw_combined = raw_results + wild_results_cache

    # 1. 第一轮提取并清洗所有合法的 candidate 元组，并保留原始数据
    unique_entries = set()
    all_extracted_candidates = []
    invalid_count = 0
    duplicate_count = 0

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

        all_extracted_candidates.append((host, port))

    # 2. 对泛域名模版匹配进行统计。找出所有的前缀，并在各后缀分组中统计出频次最高的“王者前缀”
    wildcard_suffixes = [wd.replace('*', '') for wd in WILDCARD_DOMAINS]
    prefix_frequency_tracker = {suffix: Counter() for suffix in wildcard_suffixes}

    for host, port in all_extracted_candidates:
        host_lower = host.lower()
        for suffix in wildcard_suffixes:
            # 匹配后缀，如: xxxx.cf.090227.xyz 匹配 .cf.090227.xyz
            if host_lower.endswith(suffix):
                prefix = host_lower[:-len(suffix)]
                # 排除 cf 默认值、空、非合法前缀字符，高精度统计频数
                if prefix and prefix != 'cf' and is_valid_domain(prefix):
                    prefix_frequency_tracker[suffix][prefix] += 1
            elif host_lower == suffix[1:]: # 例如裸域名 cf.090227.xyz
                prefix_frequency_tracker[suffix]['cf'] += 1

    # 计算出各个后缀出现频次最高（Winner）的最热前缀，若无或有并列，则退化使用默认的 cf
    suffix_to_winner_prefix = {}
    print("👑 [王者竞争中] 开始对提取的域名匹配组计算泛域名的最热前缀：")
    for suffix in wildcard_suffixes:
        counts = prefix_frequency_tracker[suffix]
        if not counts:
            suffix_to_winner_prefix[suffix] = "cf"
            print(f"    - 后缀 {suffix.ljust(25)} : 无可用提取前缀，统一设为默认前缀 [cf]")
        else:
            max_frequency = max(counts.values())
            # 统计最高频次的前缀得主
            winners = [k for k, v in counts.items() if v == max_frequency]
            if len(winners) == 1:
                suffix_to_winner_prefix[suffix] = winners[0]
                print(f"    - 后缀 {suffix.ljust(25)} : 王者前缀为 [{winners[0]}] (统计频次高达: {max_frequency} 次)")
            else:
                suffix_to_winner_prefix[suffix] = "cf"
                print(f"    - 后缀 {suffix.ljust(25)} : 存在并列高频前缀 (包含{winners})，统一退守设为 [cf]")

    # 3. 第二轮处理：将所有 matching 域名强制重写为最热前缀域名，并进行精洗与去重
    ip_records = []
    crawled_domains = []

    for host, port in all_extracted_candidates:
        host_lower = host.lower()
        
        # 匹配泛域名并强制重写
        for suffix in wildcard_suffixes:
            if host_lower.endswith(suffix) or host_lower == suffix[1:]:
                best_prefix = suffix_to_winner_prefix.get(suffix, "cf")
                host = f"{best_prefix}{suffix}"
                break

        standard_format = f"{host} {port}"
        if standard_format in unique_entries:
            duplicate_count += 1
            continue

        unique_entries.add(standard_format)

        if is_ip(host):
            ip_records.append((host, port))
        else:
            if not is_valid_domain(host):
                invalid_count += 1
                continue
            crawled_domains.append((host, port))

    # ==================== 7. 标准输出非标优选排序组合 ====================
    final_output_lines = []

    # 1. 开头置顶：指定静态特定域名
    for static_d in STATIC_DOMAINS:
        final_output_lines.append(f"{static_d} 443")

    # 2. 开头置顶：固定泛域名统一转为选出的最热前缀形式
    for wd in WILDCARD_DOMAINS:
        suffix = wd.replace('*', '')
        best_prefix = suffix_to_winner_prefix.get(suffix, "cf")
        formatted_wd = f"{best_prefix}{suffix}"
        final_output_lines.append(f"{formatted_wd} 443")

    # 3. 追加去重排序后的纯 IP
    for ip, p in sorted(ip_records):
        final_output_lines.append(f"{ip} {p}")

    # 4. 结尾：追加其余未重复的普通订阅域名
    written_nodes = set()
    cleaned_output = []

    for node_line in final_output_lines:
        cleaned_output.append(node_line)
        written_nodes.add(node_line)

    for d, p in sorted(crawled_domains):
        standard_node = f"{d} {p}"
        if standard_node not in written_nodes:
            cleaned_output.append(standard_node)
            written_nodes.add(standard_node)

    # ==================== 8. 落盘并向 log.txt 智能追加总结日志 ====================
    try:
        # 写入 juhe.txt
        with open(file_all, "w", encoding="utf-8") as f:
            for line in cleaned_output:
                f.write(line + "\n")

        elapsed = time.time() - start_time

        # 智能自增算第几次聚合
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

        # 优雅极致缩略的总结面板
        dashboard = []
        dashboard.append("===========================================")
        dashboard.append(f"[北京时间] {time_str}")
        dashboard.append(f"{log_header}")
        dashboard.append("-------------------------------------------")
        dashboard.append(f" ✨ 极速聚合去重处理完毕！(整体耗时: {elapsed:.2f} 秒)")
        dashboard.append(f" 💾 输出文件: juhe.txt")
        dashboard.append(f" 💾 追加写入日志文件: log.txt")
        dashboard.append("-------------------------------------------")
        dashboard.append(f" 📈 原始读取总行数/节点数: {total_raw_crawled_count} 个")
        dashboard.append(f" 🚫 过滤无效占位/异常节点数: {invalid_count} 个")
        dashboard.append(f" 🔄 除去重复/冗余重叠节点数: {duplicate_count} 个")
        dashboard.append(f" 💾 最终去重保留节点总量: {len(cleaned_output)} 个")
        dashboard.append("===========================================\n\n")

        full_dashboard_text = "\n".join(dashboard)
        
        # 终端实时渲染
        print(full_dashboard_text)

        # 追加落盘
        with open(file_log, "a", encoding="utf-8") as f_log:
            f_log.write(full_dashboard_text)

    except Exception as e:
        print(f"❌ 落盘保存失败！错误原因: {e}")


if __name__ == "__main__":
    main()