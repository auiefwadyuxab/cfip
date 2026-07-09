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
from collections import Counter

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

# 从 GitHub 环境变量/密钥中动态引入订阅链接
WILD_SUB_URL = os.environ.get("WILD_SUB_URL", "").strip()

# 自动解析 WILD_SUB_URL，动态提取 Host 参数
WILD_SUB_HOST = ""
if WILD_SUB_URL:
    try:
        parsed_url = urlparse(WILD_SUB_URL)
        WILD_SUB_HOST = parsed_url.netloc
    except Exception as e:
        print(f"⚠️ 解析 WILD_SUB_URL Host 出错: {e}")

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

# 精准过滤无效的主机占位符或系统默认 Host
exact_block_hosts = {"127.0.0.1", "localhost", "0.0.0.0", "::1", "::"}
if WILD_SUB_HOST:
    exact_block_hosts.add(WILD_SUB_HOST)

substring_block_keywords = [
    "too many requests", "error", "bestcf.pages.dev", "exception"
]

# ==================== 4. 编译正则表达式 ====================
ipv4_regex = re.compile(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$')
html_tag_regex = re.compile(r'<[^>]+>')


# ==================== 5. 核心辅助算法与分类引擎 ====================
def clean_comment_and_spaces(text_line):
    """彻底过滤并删除 # 符号及其后面的所有备注信息"""
    if '#' in text_line:
        text_line = text_line.split('#')[0]
    return text_line.strip()


def extract_pure_ip_and_port(ip_port):
    """精确拆分主机名和对应端口，同时彻底抹除 IPv6 上的 [ ] 方括号"""
    ip_port = ip_port.strip()
    ip_port = re.sub(r'\s+', ' ', ip_port)

    # 1. 已经是空格分隔的非标格式
    if ' ' in ip_port:
        parts = ip_port.rsplit(' ', 1)
        if parts[1].isdigit():
            return parts[0].replace('[', '').replace(']', '').strip(), parts[1].strip()
        return ip_port.replace('[', '').replace(']', '').strip(), "443"

    # 2. 带方括号的标准 IPv6 地址形式
    if ip_port.startswith('['):
        if ']' in ip_port:
            parts = ip_port.split(']')
            host = parts[0][1:].strip()
            remaining = parts[1].strip()
            port = remaining[1:].strip() if remaining.startswith(':') else "443"
            return host, port or "443"
        return ip_port.replace('[', '').replace(']', '').strip(), "443"

    # 3. 不带中括号但多于 1 个冒号的原始 IPv6
    if ip_port.count(':') > 1:
        parts = ip_port.rsplit(':', 1)
        if parts[1].isdigit() and 1 <= int(parts[1]) <= 65535:
            return parts[0].strip(), parts[1].strip()
        return ip_port.strip(), "443"

    # 4. 标准冒号分隔 (IPv4:port 或 domain:port)
    if ':' in ip_port:
        parts = ip_port.rsplit(':', 1)
        return parts[0].strip(), parts[1].strip()

    return ip_port, "443"


def is_ip(address):
    """验证是否是纯 IP 形式 (包含IPv4与IPv6)"""
    if ipv4_regex.match(address):
        return True
    if ':' in address:
        return True
    return False


def is_valid_domain(hostname):
    """深度审查域名格式的合法性"""
    if not hostname or len(hostname) > 255:
        return False
    if hostname.replace(".", "").isdigit():
        return False
    if ".." in hostname or hostname.startswith(".") or hostname.endswith("."):
        return False
    return bool(re.match(r'^[a-zA-Z0-9][-a-zA-Z0-9.]*[a-zA-Z0-9]$', hostname))


def parse_sharing_link(line):
    """高性能解包和洗净 Vmess/Vless/Trojan/SS 节点分享链接"""
    line = line.strip()
    if not line:
        return None

    # 处理 Vmess 的 Base64 解包
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

    # 处理 Vless / Trojan / SS
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
    """自适应 Base64 安全自愈解码，支持整体解析与逐行逃逸双层机制"""
    raw_content_stripped = raw_content.strip()
    if not raw_content_stripped:
        return ""

    if "vless://" in raw_content_stripped or "vmess://" in raw_content_stripped:
        return raw_content_stripped

    # 尝试整体统一解密
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

    # 逐行自愈解密 (防不规范断行污染)
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
    """内容自适应精洗深度解析引擎：脱除 HTML 干扰，Base64 自愈转换，JSON 降级，节点分享链解析"""
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
    """单源抓取。针对重度聚合Wild源给予高带宽长超时连接支持，其他普通源给予极速阻断策略"""
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

    # 如果是Wild重度聚合源，超时放宽至 15秒连接，90秒传输，防ReadTimeout
    # 常规源则是 2秒连接，5秒传输
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

            # CF盾阻断验证与异常 HTML 页面抛弃过滤
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
            # 记录具体的超时原因
            print(f"[快速阻断/离线] -> {filename} ({type(e).__name__})")
    return url, local_lines


# ==================== 6. 主逻辑调度与统计合并管理 ====================
def main():
    start_time = time.time()
    
    # 获取精确的北京时间 (UTC+8)
    tz_beijing = timezone(timedelta(hours=8))
    bj_now = datetime.now(timezone.utc).astimezone(tz_beijing)
    time_str = bj_now.strftime('%Y-%m-%d %H:%M:%S')

    raw_results = []
    session = requests.Session()

    # 13个源提供商分布统计所需的数据结构
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
    
    # 统计记录容器
    raw_source_counter = Counter()     # 原始各优选订阅器占比
    unique_source_counter = Counter()  # 实际最终保留各优选订阅器占比

    print(f"==========================================")
    print(f"📡 极速无缝聚合去重任务开启 | 启动时间: {time_str}")
    print(f"==========================================")

    # 第一阶段：首先拉取 Wild 聚合源并缓存
    wild_results_cache = []
    if WILD_SUB_URL:
        print(f"🔗 [第一阶段] 启动 Wild 聚合源爬取 -> 动态 Host: {WILD_SUB_HOST} (配予重度长超时机制)...")
        _, fetched_lines = fetch_single_url(session, WILD_SUB_URL, WILD_SUB_HOST, is_wild_source=True)
        if fetched_lines:
            wild_results_cache = fetched_lines
            print(f"✅ Wild 聚合源爬取成功，已安全缓存 {len(wild_results_cache)} 条原始未清洗记录！")
        else:
            print("❌ 警告：Wild 聚合源拉取数据为空或发生超时（将仅合并其余 54 个常规优选源）。")
        
        # 平滑过渡停顿 1.0s，抗阻断保护
        print("🕒 执行机器缓冲停顿以保证 API 安全连接稳定性...")
        time.sleep(1.0)
    else:
        print("💡 提示：未检测到 WILD_SUB_URL 密钥环境变量。跳过第一阶段 Wild 订阅爬取。")

    # 第二阶段：极致多线程并轨拉取常规订阅（ALL_SOURCES）
    max_fetch_workers = len(ALL_SOURCES)
    print(f"\n🚀 [第二阶段] 开启多线程并轨极速拉取常规订阅 (并发线程数: {max_fetch_workers})...")
    
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

    # 第三阶段：进行数据洗净、泛域名 cf 前缀强制替换与严格去重
    print("\n🧐 [第三阶段] 正在将缓存数据深度合并，启动信息去重过滤机制...")
    
    all_raw_combined = raw_results + wild_results_cache
    total_raw_crawled_count = len(all_raw_combined)

    unique_entries = set()
    invalid_count = 0
    duplicate_count = 0

    # 提取泛域名后缀主干 (即去掉前面的 *) -> '.cf.090227.xyz', '.tencentapp.cn', '.cloudflare.182682.xyz'
    wildcard_suffixes = [wd.replace('*', '') for wd in WILDCARD_DOMAINS]

    # IP 与普通域名的清洗重整分类容器
    ip_records = []
    crawled_domains = []

    for line in all_raw_combined:
        # 分布逆向追踪归属 (用于生成 13 个优选源的统计贡献图报表)
        detected_source = "未知常规优选源"
        line_decoded = line.lower()
        for source_name, keywords in source_mapping.items():
            if any(kw in line_decoded for kw in keywords):
                detected_source = source_name
                break
        raw_source_counter[detected_source] += 1

        # 核心清洗 #
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

        # 泛域名后缀映射拦截：
        # 只要属于这个泛域名的子域（如 xxx.cf.090227.xyz），或完全等同于其后缀裸域名，统一将其前缀修改重构为 cf。
        matched_any_wildcard = False
        for suffix in wildcard_suffixes:
            if host_lower.endswith(suffix) or host_lower == suffix[1:]:
                # 强制转换为唯一的 cf.{suffix}（即 cf.cf.090227.xyz）
                host = f"cf{suffix}"
                matched_any_wildcard = True
                break

        # 生成标准的非标优选格式进行严格去重
        standard_format = f"{host} {port}"
        if standard_format in unique_entries:
            duplicate_count += 1
            continue

        unique_entries.add(standard_format)
        unique_source_counter[detected_source] += 1

        # 整理分类
        if is_ip(host):
            ip_records.append((host, port))
        else:
            if not is_valid_domain(host):
                invalid_count += 1
                continue
            crawled_domains.append((host, port))

    # ==================== 7. 按标准格式拼装最终排版顺序 ====================
    final_output_lines = []

    # 1. 开头置顶：静态特有域名
    for static_d in STATIC_DOMAINS:
        final_output_lines.append(f"{static_d} 443")

    # 2. 开头置顶：固定泛域名统一将 * 号替换为 cf 前缀
    for wd in WILDCARD_DOMAINS:
        formatted_wd = wd.replace('*', 'cf')
        final_output_lines.append(f"{formatted_wd} 443")

    # 3. 追加去重后的 纯 IP 节点 (IPv4 与 IPv6 分流排序)
    for ip, p in sorted(ip_records):
        final_output_lines.append(f"{ip} {p}")

    # 4. 追加末尾：剩余的所有常规订阅域名
    # 双重安全，防止跟开头泛域名有交集
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

    # ==================== 8. 落盘物理存储并智能生成双份极客日志 ====================
    try:
        # 1. 写入 juhe.txt
        with open(file_all, "w", encoding="utf-8") as f:
            for line in cleaned_output:
                f.write(line + "\n")

        elapsed = time.time() - start_time

        # 2. 智能计算当前是第几次聚合运行
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

        # 3. 完美结合两份脚本在终端的输出报表
        dashboard = []
        dashboard.append("===========================================")
        dashboard.append(f"[北京时间] {time_str}")
        dashboard.append(f"{log_header}")
        dashboard.append("-------------------------------------------")
        dashboard.append(f" ✨ 极速聚合去重处理完毕！(耗时: {elapsed:.2f}s)")
        dashboard.append(f" 💾 最终输出非标优选路径: juhe.txt")
        dashboard.append(f" 💾 本次运行日志追加至: log.txt")
        dashboard.append(f" 📈 原始读取总记录/行数: {total_raw_crawled_count} 个")
        dashboard.append(f" 🚫 过滤无效/本地环路节点数: {invalid_count} 个")
        dashboard.append(f" 🔄 除去重复/冗余节点数: {duplicate_count} 个")
        dashboard.append(f" 💾 最终去重保留节点总量: {len(cleaned_output)} 个")
        dashboard.append("-------------------------------------------")
        dashboard.append(" 📶 13 个主流重度订阅器及常规源节点贡献分布统计 (原始拉取 -> 实际保留):")
        
        # 依次打印 13 个专业提供商的精准分布
        all_sources = ["S5公益", "Moist_R", "CM", "Mia", "天诚1", "洛璃", "辣子鸡", "辣椒炒肉少放辣", "文烨", "Kristi", "周润发", "IDK", "DanFeng"]
        for src in all_sources:
            raw_c = raw_source_counter.get(src, 0)
            uniq_c = unique_source_counter.get(src, 0)
            dashboard.append(f"    - {src.ljust(15)} : {str(raw_c).rjust(4)} -> {str(uniq_c).rjust(4)} 个节点")
            
        other_raw = raw_source_counter.get("未知常规优选源", 0)
        other_uniq = unique_source_counter.get("未知常规优选源", 0)
        if other_raw > 0:
            dashboard.append(f"    - {'54个常规优选源'.ljust(15)} : {str(other_raw).rjust(4)} -> {str(other_uniq).rjust(4)} 个节点")
            
        dashboard.append("===========================================\n\n")

        # 将仪表盘拼接为文本
        full_dashboard_text = "\n".join(dashboard)
        
        # 同时打印在控制台终端
        print(full_dashboard_text)

        # 追加式写入 log.txt
        with open(file_log, "a", encoding="utf-8") as f_log:
            f_log.write(full_dashboard_text)

    except Exception as e:
        print(f"❌ 最终刷写落盘保存失败！原因: {e}")


if __name__ == "__main__":
    main()