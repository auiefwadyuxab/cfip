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


# ==================== 5. 核心辅助算法引擎 ====================
def clean_comment_and_spaces(text_line):
    """彻底过滤并删除 # 符号及其后面的所有备注信息"""
    if '#' in text_line:
        text_line = text_line.split('#')[0]
    return text_line.strip()


def extract_pure_ip_and_port(ip_port):
    """精确拆分主机名和对应端口，同时彻底抹除 IPv6 上的 [ ] 方括号"""
    ip_port = ip_port.strip()
    ip_port = re.sub(r'\s+', ' ', ip_port)

    # 已经是空格分隔的非标格式
    if ' ' in ip_port:
        parts = ip_port.rsplit(' ', 1)
        if parts[1].isdigit():
            return parts[0].replace('[', '').replace(']', '').strip(), parts[1].strip()
        return ip_port.replace('[', '').replace(']', '').strip(), "443"

    # 带方括号的标准 IPv6 地址形式
    if ip_port.startswith('['):
        if ']' in ip_port:
            parts = ip_port.split(']')
            host = parts[0][1:].strip()
            remaining = parts[1].strip()
            port = remaining[1:].strip() if remaining.startswith(':') else "443"
            return host, port or "443"
        return ip_port.replace('[', '').replace(']', '').strip(), "443"

    # 不带中括号但多于 1 个冒号的原始 IPv6
    if ip_port.count(':') > 1:
        parts = ip_port.rsplit(':', 1)
        if parts[1].isdigit() and 1 <= int(parts[1]) <= 65535:
            return parts[0].strip(), parts[1].strip()
        return ip_port.strip(), "443"

    # 标准冒号分隔 (IPv4:port 或 domain:port)
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


def fetch_single_url(session, url, custom_host=None):
    """多线程单 URL 极速爬取，设置激进超时阻断，保障极致拉取速度"""
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

            # CF盾阻断验证与异常 HTML 页面抛弃过滤
            if 'text/html' in content_type or text_preview.startswith('<!doctype html') or text_preview.startswith('<html'):
                with print_lock:
                    print(f"⚠️ [拦截] 源 {filename} 触发了 Cloudflare 盾质询，已自动抛弃 HTML 数据。")
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


# ==================== 6. 主逻辑调度与时间节点管理 ====================
def main():
    start_time = time.time()
    
    # 获取精确的北京时间 (UTC+8)
    tz_beijing = timezone(timedelta(hours=8))
    bj_now = datetime.now(timezone.utc).astimezone(tz_beijing)
    time_str = bj_now.strftime('%Y-%m-%d %H:%M:%S')

    raw_results = []
    session = requests.Session()

    print(f"==========================================")
    print(f"📡 极速聚合去重任务开启 | 启动时间: {time_str}")
    print(f"==========================================")

    # 1. 触发动态聚合订阅源（Wild）并缓存
    wild_results_cache = []
    if WILD_SUB_URL:
        print(f"🔗 [第一阶段] 启动 Wild 聚合源爬取 -> 动态 Host: {WILD_SUB_HOST}...")
        _, fetched_lines = fetch_single_url(session, WILD_SUB_URL, WILD_SUB_HOST)
        if fetched_lines:
            wild_results_cache = fetched_lines
            print(f"✅ Wild 聚合源成功拉取并缓存，共 {len(wild_results_cache)} 条临时节点")
        else:
            print("❌ 警告：未拉取到 Wild 聚合源数据，或拉取到的内容为空。")
        
        # 机器平滑缓冲停顿，平滑抗封防封频
        print("🕒 缓冲停顿中，保证 API 连接稳定性与安全性...")
        time.sleep(1.0)
    else:
        print("💡 提示：未检测到 WILD_SUB_URL 密钥环境变量。跳过 Wild 聚合拉取。")

    # 2. 并行拉取常规订阅（ALL_SOURCES）
    max_fetch_workers = len(ALL_SOURCES)
    print(f"\n🚀 [第二阶段] 解除并发阀门，开启常规订阅激进拉取 (线程数: {max_fetch_workers})...")
    
    adapter = requests.adapters.HTTPAdapter(pool_connections=max_fetch_workers, pool_maxsize=max_fetch_workers * 2)
    session.mount('http://', adapter)
    session.mount('https://', adapter)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_fetch_workers) as executor:
        future_to_url = {executor.submit(fetch_single_url, session, url): url for url in ALL_SOURCES}
        for future in concurrent.futures.as_completed(future_to_url):
            try:
                _, res_lines = future.result()
                if res_lines:
                    raw_results.extend(res_lines)
            except Exception:
                pass
    session.close()

    # 3. 将缓存合并并进行深度统一去重过滤
    print("\n🧐 [第三阶段] 正在将缓存数据深度合并，启动信息去重过滤机制...")
    total_raw_crawled_count = len(raw_results) + len(wild_results_cache)

    all_raw_combined = raw_results + wild_results_cache
    unique_entries = set()
    invalid_count = 0
    duplicate_count = 0

    # 泛域名匹配主干（用于在去重时将其后缀的前缀重写为 cf）
    wildcard_suffixes = [wd.replace('*', '') for wd in WILDCARD_DOMAINS]

    # IP 与域名的二次清洗容器
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

        # 泛域名后缀自动标准化匹配重写：
        # 如果提取出的域名后缀匹配泛域名列表，其前缀强制统一映射重构为 cf。
        matched_any_wildcard = False
        for suffix in wildcard_suffixes:
            if host_lower.endswith(suffix) or host_lower == suffix[1:]:
                # 重新映射为 cf.{suffix}
                host = f"cf{suffix}"
                matched_any_wildcard = True
                break

        # 生成标准化格式进行严格去重
        standard_format = f"{host} {port}"
        if standard_format in unique_entries:
            duplicate_count += 1
            continue

        unique_entries.add(standard_format)

        # 整理分类
        if is_ip(host):
            ip_records.append((host, port))
        else:
            if not is_valid_domain(host):
                invalid_count += 1
                continue
            
            # 由于在前面匹配中泛域名的前缀已经被转为 cf，因此不需要在 crawled_domains 里再次重复匹配
            crawled_domains.append((host, port))

    # ==================== 7. 按规范严控输出排布 ====================
    final_output_lines = []

    # 1. 置顶静态域名
    for static_d in STATIC_DOMAINS:
        final_output_lines.append(f"{static_d} 443")

    # 2. 固定泛域名的 * 替换为规范前缀 cf
    for wd in WILDCARD_DOMAINS:
        formatted_wd = wd.replace('*', 'cf')
        final_output_lines.append(f"{formatted_wd} 443")

    # 3. 追加去重后的 IP 节点
    for ip, p in sorted(ip_records):
        final_output_lines.append(f"{ip} {p}")

    # 4. 结尾：追加剩余的所有优选域名
    # 注意：泛域名重写后可能和普通域名重复，因此统一再次去重写入
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

    # ==================== 8. 落盘并智能维护 log.txt 日志 ====================
    try:
        # 写入 juhe.txt
        with open(file_all, "w", encoding="utf-8") as f:
            for line in cleaned_output:
                f.write(line + "\n")

        elapsed = time.time() - start_time

        # 智能计算是第几次聚合运行
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

        # 简练、合并的控制台与追加日志面板
        dashboard = (
            f"===========================================\n"
            f"[北京时间] {time_str}\n"
            f"{log_header}\n"
            f"-------------------------------------------\n"
            f" ✨ 极速聚合去重处理完毕！(耗时: {elapsed:.2f}s)\n"
            f" 💾 输出文件: juhe.txt\n"
            f" 💾 本次日志已追加至: log.txt\n"
            f" 📈 原始读取总节点数: {total_raw_crawled_count} 个\n"
            f" 🚫 过滤无效/本地环路节点数: {invalid_count} 个\n"
            f" 🔄 除去重复/冗余节点数: {duplicate_count} 个\n"
            f" 💾 最终生成非标优选节点总量: {len(cleaned_output)} 个\n"
            f"===========================================\n\n"
        )

        print(dashboard)

        # 刷入 log.txt
        with open(file_log, "a", encoding="utf-8") as f_log:
            f_log.write(dashboard)

    except Exception as e:
        print(f"❌ 最终刷写落盘保存失败！原因: {e}")


if __name__ == "__main__":
    main()