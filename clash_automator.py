import asyncio
import yaml
import aiohttp
import urllib.parse
import os
import sys
import base64
import json
from utils.config_loader import load_config
from core.ip_checker import IPChecker

# --- CONFIGURATION ---
cfg = load_config("config.yaml") or {}
# 这里的 config.yaml 是写死的，对应 workflow
CLASH_CONFIG_PATH = cfg.get('yaml_path', "config.yaml") 
CLASH_API_URL = cfg.get('clash_api_url', "http://127.0.0.1:9097")
CLASH_API_SECRET = cfg.get('clash_api_secret', "")
SELECTOR_NAME = cfg.get('selector_name', "GLOBAL")
OUTPUT_SUFFIX = cfg.get('output_suffix', "_checked")

# 测速配置
SPEED_TEST_URL = "http://www.gstatic.com/generate_204"
SPEED_TEST_TIMEOUT = 5000 # 5000ms 超时,提高高延迟节点通过率

class ClashController:
    def __init__(self, api_url, secret=""):
        self.api_url = api_url.rstrip('/')
        self.headers = {
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/json"
        }

    async def switch_proxy(self, selector, proxy_name):
        url = f"{self.api_url}/proxies/{urllib.parse.quote(selector)}"
        payload = {"name": proxy_name}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.put(url, json=payload, headers=self.headers, timeout=5) as resp:
                    return resp.status == 204
        except Exception as e:
            print(f"API Error switching to {proxy_name}: {e}")
            return False

    async def set_mode(self, mode):
        url = f"{self.api_url}/configs"
        payload = {"mode": mode}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.patch(url, json=payload, headers=self.headers, timeout=5) as resp:
                    return resp.status == 204
        except Exception:
            return False

    async def get_proxy_delay(self, proxy_name):
        """
        调用 Clash API 测试单个节点延迟
        返回: 延迟(ms) 或 None (失败)
        """
        encoded_name = urllib.parse.quote(proxy_name)
        url = f"{self.api_url}/proxies/{encoded_name}/delay"
        params = {
            "timeout": str(SPEED_TEST_TIMEOUT),
            "url": SPEED_TEST_URL
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=self.headers, params=params, timeout=5) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get('delay')
                    else:
                        return None
        except Exception:
            return None

async def process_proxies():
    print(f"Loading config from: {CLASH_CONFIG_PATH}")
    if not os.path.exists(CLASH_CONFIG_PATH):
        print(f"Error: Config file not found at {CLASH_CONFIG_PATH}")
        return

    try:
        with open(CLASH_CONFIG_PATH, 'r', encoding='utf-8') as f:
            config_data = yaml.full_load(f)
    except Exception as e:
        print(f"Error parsing YAML: {e}")
        return

    proxies = config_data.get('proxies', [])
    if not proxies:
        print("No 'proxies' found in config.")
        return

    SKIP_KEYWORDS = ["剩余", "重置", "到期", "有效期", "官网", "网址", "更新", "公告"]
    
    controller = ClashController(CLASH_API_URL, CLASH_API_SECRET)
    
    # --- 阶段 1: 快速连通性测试 (新增功能) ---
    print(f"\n🚀 [Phase 1] Starting Connectivity Test for {len(proxies)} nodes...")
    print(f"   Timeout: {SPEED_TEST_TIMEOUT}ms | URL: {SPEED_TEST_URL}")
    
    valid_proxies = []
    
    # 限制并发数，防止把 Clash 冲垮
    semaphore = asyncio.Semaphore(50) 

    async def check_node(proxy):
        name = proxy['name']
        # 关键词过滤
        for kw in SKIP_KEYWORDS:
            if kw in name:
                return None
        
        async with semaphore:
            delay = await controller.get_proxy_delay(name)
            if delay:
                print(f"   ✅ {delay}ms | {name}")
                return proxy
            else:
                print(f"   ❌ Timeout | {name}")
                return None

    tasks = [check_node(p) for p in proxies]
    results = await asyncio.gather(*tasks)
    
    # 过滤掉 None
    valid_proxies = [p for p in results if p is not None]
    
    print(f"\n📊 [Phase 1 Summary] Total: {len(proxies)} -> Alive: {len(valid_proxies)}")
    print("---------------------------------------------------")

    if not valid_proxies:
        print("No valid proxies left after speed test. Exiting.")
        return

    # --- 阶段 1.5: IP 预检测去重 ---
    print(f"\n🔄 [Phase 1.5] Pre-checking IPs for deduplication...")
    
    # 强制全局模式
    await controller.set_mode("global")
    
    # 获取端口
    mixed_port = 7890
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{CLASH_API_URL}/configs", headers=controller.headers) as resp:
                if resp.status == 200:
                    conf = await resp.json()
                    if conf.get('mixed-port', 0) != 0: mixed_port = conf['mixed-port']
    except Exception:
        pass

    local_proxy_url = f"http://127.0.0.1:{mixed_port}"
    print(f"Using Local Proxy: {local_proxy_url}")
    
    # 确定 Selector (通常是 GLOBAL)
    selector_to_use = SELECTOR_NAME
    # (省略了复杂的 selector 检测逻辑，直接尝试 GLOBAL，失败则尝试 Proxy)
    # 简单的 fallback 逻辑
    if not await controller.switch_proxy("GLOBAL", valid_proxies[0]['name']):
        selector_to_use = "Proxy"

    # IP去重逻辑
    ip_to_proxy = {}  # IP -> 第一个使用该IP的proxy
    unique_proxies = []
    
    # 新增：记录每个节点的IP状态（用于Phase 2优化）
    node_ip_map = {}  # name -> ip (or None if failed)
    
    # 创建临时checker用于快速IP检测
    temp_checker = IPChecker(headless=True)
    await temp_checker.start()
    
    try:
        # 串行逐个检测，给IP池充足的轮询时间
        for i, proxy in enumerate(valid_proxies):
            name = proxy['name']
            source = proxy.get('_source', 'Unknown')
            print(f"   [{i+1}/{len(valid_proxies)}] Checking: {name} ({source})")
            
            # 切换节点
            if not await controller.switch_proxy(selector_to_use, name):
                print(f"      -> Switch failed, keeping node.")
                unique_proxies.append(proxy)
                continue

            # 等待切换生效，给IP池时间轮询
            await asyncio.sleep(1.5)
            
            # 快速获取IP
            ip = await temp_checker.get_simple_ip(local_proxy_url)
            
            # 记录IP映射（用于Phase 2优化）
            node_ip_map[name] = ip  # 可能是 None
            
            if ip:
                if ip not in ip_to_proxy:
                    # 第一次见到这个IP，保留
                    ip_to_proxy[ip] = proxy
                    unique_proxies.append(proxy)
                    print(f"      ✅ {ip} | {name}")
                else:
                    # 重复IP，判断是否跨订阅
                    duplicate_proxy = ip_to_proxy[ip]
                    duplicate_name = duplicate_proxy['name']
                    duplicate_source = duplicate_proxy.get('_source', 'Unknown')
                    current_source = proxy.get('_source', 'Unknown')
                    
                    if duplicate_source == current_source:
                        # 同订阅内IP重复 = IP池共享，仍然保留
                        unique_proxies.append(proxy)
                        print(f"      ✅ {ip} | {name}")
                        print(f"         └─ 同订阅IP池共享 ({duplicate_source})")
                    else:
                        # 跨订阅IP重复 = 真正的节点重复，才去重
                        print(f"      ⏭️ {ip} | 跨订阅重复，已去重")
                        print(f"         ✅ 保留: {duplicate_name} ({duplicate_source})")
                        print(f"         ❌ 丢弃: {name} ({current_source})")
            else:
                # IP获取失败的也保留，后续浏览器检测
                unique_proxies.append(proxy)
                print(f"      ❓ Unknown IP | {name}")
    finally:
        await temp_checker.stop()
    
    print(f"\n📊 [Phase 1.5 Summary] Unique IPs: {len(unique_proxies)} / {len(valid_proxies)}")
    
    # --- 阶段 2: IP 纯净度检查 (优化版：三层优化策略) ---
    print(f"\n🕵️ [Phase 2] Starting IP Purity Check (Optimized)...")
    
    # 统计信息
    stats_skipped = 0    # 跳过的节点（IP不可用）
    stats_cached = 0     # 缓存继承的节点
    stats_detected = 0   # 实际检测的节点
    
    results_map = {}  # name -> result_suffix
    ip_result_cache = {}  # IP -> result_string (缓存复用)
    
    # 层次1 & 层次3：按IP分组，跳过失败节点
    ip_groups = {}  # IP -> list of proxies
    skipped_proxies = []  # IP获取失败的节点
    
    for proxy in unique_proxies:
        name = proxy['name']
        ip = node_ip_map.get(name)
        if ip:
            ip_groups.setdefault(ip, []).append(proxy)
        else:
            # 层次1：IP获取失败的节点直接标记为未知
            results_map[name] = "【❓❓ 未知】"
            skipped_proxies.append(name)
            stats_skipped += 1
    
    print(f"   📊 预处理统计:")
    print(f"      - 跳过 (IP不可用): {stats_skipped} 节点")
    print(f"      - 待检测唯一IP数: {len(ip_groups)} 个")
    print(f"      - 涉及节点总数: {len(unique_proxies) - stats_skipped} 个")
    
    if skipped_proxies:
        print(f"\n   ⏭️ 跳过的节点 (Phase 1.5 IP获取失败):")
        for name in skipped_proxies[:5]:  # 只显示前5个
            print(f"      - {name}")
        if len(skipped_proxies) > 5:
            print(f"      ... 及其他 {len(skipped_proxies) - 5} 个节点")
    
    checker = IPChecker(headless=True)
    await checker.start()

    try:
        # 层次3：每个IP只检测一个代表节点
        ip_list = list(ip_groups.keys())
        for i, ip in enumerate(ip_list):
            group = ip_groups[ip]
            representative = group[0]  # 取第一个作为代表
            representative_name = representative['name']
            
            print(f"\n[{i+1}/{len(ip_list)}] 检测IP: {ip}")
            print(f"   代表节点: {representative_name}")
            if len(group) > 1:
                print(f"   同IP节点: {len(group)} 个 (将继承结果)")
            
            # 切换到代表节点
            if not await controller.switch_proxy(selector_to_use, representative_name):
                print("   ❌ 代理切换失败，标记为未知")
                result = "【❓❓ 未知】"
            else:
                await asyncio.sleep(1)  # 层次2：从2秒优化到1秒
                
                # 检测IP纯净度
                res = None
                try:
                    res = await checker.check(proxy=local_proxy_url, timeout=10000)  # 层次2：超时优化
                    if res.get('error') is None and res.get('pure_score') != '❓':
                        result = res.get('full_string', "【❓❓ 未知】")
                    else:
                        result = res.get('full_string', "【❓❓ 未知】")
                except Exception as e:
                    print(f"   ⚠️ 检测异常: {e}")
                    result = "【❓❓ 未知】"
                
                stats_detected += 1
            
            # 缓存结果
            ip_result_cache[ip] = result
            
            # 传播结果到所有同IP节点
            for proxy in group:
                name = proxy['name']
                results_map[name] = result
                if name != representative_name:
                    stats_cached += 1
            
            # 显示结果
            print(f"   ✅ 结果: {result}")
            if len(group) > 1:
                inherited_names = [p['name'] for p in group[1:]]
                for inherited_name in inherited_names[:3]:
                    print(f"      ↳ 缓存继承: {inherited_name}")
                if len(inherited_names) > 3:
                    print(f"      ↳ ... 及其他 {len(inherited_names) - 3} 个节点")

    except KeyboardInterrupt:
        print("\nInterrupted. Saving...")
    finally:
        await checker.stop()
    
    # 输出Phase 2统计
    print(f"\n📊 [Phase 2 Summary - 优化效果]")
    print(f"   ⏭️ 跳过 (IP不可用): {stats_skipped} 节点")
    print(f"   🔍 实际检测: {stats_detected} 个唯一IP")
    print(f"   💾 缓存继承: {stats_cached} 节点")
    print(f"   📈 检测效率: 检测 {stats_detected} 次覆盖 {len(unique_proxies)} 节点")
    if stats_detected > 0:
        print(f"   ⚡ 优化比例: {(stats_skipped + stats_cached) / len(unique_proxies) * 100:.1f}% 节点无需检测")

    # --- 阶段 3: 统计与保存 ---
    print("\n📊 [Phase 3] Generating Statistics...")
    
    # 统计各等级节点数量
    stats = {
        "excellent": 0,  # ⚪ 极佳
        "good": 0,       # 🟢 优秀
        "fair": 0,       # 🟡 良好
        "medium": 0,     # 🟠 中等
        "poor": 0,       # 🔴 差
        "bad": 0,        # ⚫ 极差
        "unknown": 0,    # ❓ 未知
        "residential": 0, # 住宅IP
        "datacenter": 0,  # 机房IP
        "native": 0,      # 原生IP
        "broadcast": 0    # 广播IP
    }

    for name, result_str in results_map.items():
        # 统计纯净度
        if "⚪" in result_str: stats["excellent"] += 1
        elif "🟢" in result_str: stats["good"] += 1
        elif "🟡" in result_str: stats["fair"] += 1
        elif "🟠" in result_str: stats["medium"] += 1
        elif "🔴" in result_str: stats["poor"] += 1
        elif "⚫" in result_str: stats["bad"] += 1
        else: stats["unknown"] += 1
        
        # 统计IP类型
        if "住宅" in result_str: stats["residential"] += 1
        elif "机房" in result_str: stats["datacenter"] += 1
        
        # 统计IP来源
        if "原生" in result_str: stats["native"] += 1
        elif "广播" in result_str: stats["broadcast"] += 1

    # 输出统计报告
    print(f"""
╔══════════════════════════════════════╗
║         节点质量统计报告              ║
╠══════════════════════════════════════╣
║ 纯净度分布:                          ║
║   ⚪ 极佳: {stats['excellent']:3d}  🟢 优秀: {stats['good']:3d}     ║
║   🟡 良好: {stats['fair']:3d}  🟠 中等: {stats['medium']:3d}     ║
║   🔴 差:   {stats['poor']:3d}  ⚫ 极差: {stats['bad']:3d}     ║
║   ❓ 未知: {stats['unknown']:3d}                       ║
╠══════════════════════════════════════╣
║ IP类型: 住宅 {stats['residential']:3d} | 机房 {stats['datacenter']:3d}       ║
║ IP来源: 原生 {stats['native']:3d} | 广播 {stats['broadcast']:3d}       ║
╚══════════════════════════════════════╝
""")

    print("\n💾 Saving results...")
    
    # 我们只保存 Phase 1 存活下来的节点，并更新名字
    final_proxies = []
    name_mapping = {}

    for proxy in valid_proxies:  # 注意：这里还是用valid_proxies，因为要去重所有节点
        old_name = proxy['name']
        if old_name in results_map:
            # 方案C格式：【🟢🟠 机|广】原节点名
            result_suffix = results_map[old_name]
            
            # 直接提取【】内的完整内容作为前缀
            import re
            emoji_match = re.search(r'【([^】]+)】', result_suffix)
            if emoji_match:
                prefix = f"【{emoji_match.group(1)}】"
                new_name = f"{prefix}{old_name}"
            else:
                # 没有匹配到，使用原格式
                new_name = f"{old_name} {result_suffix}"
            
            proxy['name'] = new_name
            name_mapping[old_name] = new_name
            final_proxies.append(proxy)
        else:
            # 测速通过了，但 IP 检测没结果（可能中断了），也保留
            final_proxies.append(proxy)
    
    config_data['proxies'] = final_proxies

    # 更新 Proxy Groups (如果有的话)
    if 'proxy-groups' in config_data:
        for group in config_data['proxy-groups']:
            if 'proxies' in group:
                new_group_proxies = []
                for p_name in group['proxies']:
                    # 如果原节点被改名了，用新名字
                    if p_name in name_mapping:
                        new_group_proxies.append(name_mapping[p_name])
                    # 如果原节点没改名（说明没通过测速被删了），就不加进去
                group['proxies'] = new_group_proxies

    # 保存
    base = os.path.basename(CLASH_CONFIG_PATH)
    filename, ext = os.path.splitext(base)
    output_filename = f"{filename}{OUTPUT_SUFFIX}{ext}"
    output_path = os.path.join(os.getcwd(), output_filename)
    
    try:
        with open(output_path, 'w', encoding='utf-8') as f:
            yaml.dump(config_data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        print(f"\n✅ Clash格式已保存: {output_path}")
    except Exception as e:
        print(f"Error saving Clash config: {e}")
    
    # --- 新增：生成v2rayN格式订阅 ---
    print("\n📝 Generating v2rayN subscription...")
    v2rayn_links = []
    
    for proxy in final_proxies:
        try:
            link = convert_to_v2rayn_link(proxy)
            if link:
                v2rayn_links.append(link)
        except Exception as e:
            print(f"  ⚠️ Failed to convert {proxy['name']}: {e}")
    
    if v2rayn_links:
        # Base64编码
        v2rayn_content = '\n'.join(v2rayn_links)
        v2rayn_base64 = base64.b64encode(v2rayn_content.encode('utf-8')).decode('utf-8')
        
        # 保存v2rayN订阅文件
        v2rayn_filename = f"{filename}{OUTPUT_SUFFIX}_v2rayn.txt"
        v2rayn_path = os.path.join(os.getcwd(), v2rayn_filename)
        
        try:
            with open(v2rayn_path, 'w', encoding='utf-8') as f:
                f.write(v2rayn_base64)
            print(f"✅ v2rayN格式已保存: {v2rayn_path}")
            print(f"   节点数量: {len(v2rayn_links)}")
        except Exception as e:
            print(f"Error saving v2rayN subscription: {e}")
    else:
        print("⚠️ 没有可转换的节点用于v2rayN格式")

def convert_to_v2rayn_link(proxy):
    """
    将Clash节点配置转换为v2rayN通用订阅链接
    支持的协议: vmess, vless, trojan, ss, ssr, hysteria2
    """
    proxy_type = proxy.get('type', '').lower()
    name = proxy.get('name', 'Unknown')
    
    if proxy_type == 'vmess':
        return convert_vmess(proxy)
    elif proxy_type == 'vless':
        return convert_vless(proxy)
    elif proxy_type == 'trojan':
        return convert_trojan(proxy)
    elif proxy_type == 'ss':
        return convert_shadowsocks(proxy)
    elif proxy_type == 'ssr':
        return convert_shadowsocksr(proxy)
    elif proxy_type == 'hysteria2':
        return convert_hysteria2(proxy)
    else:
        print(f"  ⚠️ Unsupported protocol: {proxy_type} for {name}")
        return None

def convert_vmess(proxy):
    """转换VMess节点"""
    vmess_config = {
        "v": "2",
        "ps": proxy.get('name', ''),
        "add": proxy.get('server', ''),
        "port": str(proxy.get('port', '')),
        "id": proxy.get('uuid', ''),
        "aid": str(proxy.get('alterId', 0)),
        "net": proxy.get('network', 'tcp'),
        "type": proxy.get('ws-opts', {}).get('headers', {}).get('Host', 'none') if proxy.get('network') == 'ws' else 'none',
        "host": proxy.get('ws-opts', {}).get('path', '') if proxy.get('network') == 'ws' else '',
        "path": proxy.get('ws-opts', {}).get('path', '') if proxy.get('network') == 'ws' else '',
        "tls": "tls" if proxy.get('tls', False) else "",
        "sni": proxy.get('servername', ''),
        "alpn": proxy.get('alpn', [])
    }
    
    vmess_json = json.dumps(vmess_config, separators=(',', ':'))
    vmess_base64 = base64.b64encode(vmess_json.encode('utf-8')).decode('utf-8')
    return f"vmess://{vmess_base64}"

def convert_vless(proxy):
    """转换VLESS节点"""
    server = proxy.get('server', '')
    port = proxy.get('port', '')
    uuid = proxy.get('uuid', '')
    name = urllib.parse.quote(proxy.get('name', ''))
    
    params = []
    if proxy.get('network'):
        params.append(f"type={proxy['network']}")
    if proxy.get('tls'):
        params.append("security=tls")
    if proxy.get('sni'):
        params.append(f"sni={proxy['sni']}")
    
    query = '&'.join(params) if params else ''
    return f"vless://{uuid}@{server}:{port}?{query}#{name}"

def convert_trojan(proxy):
    """转换Trojan节点"""
    server = proxy.get('server', '')
    port = proxy.get('port', '')
    password = proxy.get('password', '')
    name = urllib.parse.quote(proxy.get('name', ''))
    
    params = []
    if proxy.get('sni'):
        params.append(f"sni={proxy['sni']}")
    if proxy.get('skip-cert-verify'):
        params.append("allowInsecure=1")
    
    query = '&'.join(params) if params else ''
    return f"trojan://{password}@{server}:{port}?{query}#{name}"

def convert_shadowsocks(proxy):
    """转换Shadowsocks节点"""
    server = proxy.get('server', '')
    port = proxy.get('port', '')
    method = proxy.get('cipher', '')
    password = proxy.get('password', '')
    name = urllib.parse.quote(proxy.get('name', ''))
    
    # method:password
    userinfo = f"{method}:{password}"
    userinfo_base64 = base64.b64encode(userinfo.encode('utf-8')).decode('utf-8')
    
    return f"ss://{userinfo_base64}@{server}:{port}#{name}"

def convert_shadowsocksr(proxy):
    """转换ShadowsocksR节点"""
    # SSR格式较复杂，这里提供基础实现
    server = proxy.get('server', '')
    port = proxy.get('port', '')
    protocol = proxy.get('protocol', '')
    method = proxy.get('cipher', '')
    obfs = proxy.get('obfs', '')
    password = base64.b64encode(proxy.get('password', '').encode('utf-8')).decode('utf-8')
    
    ssr_raw = f"{server}:{port}:{protocol}:{method}:{obfs}:{password}"
    ssr_base64 = base64.b64encode(ssr_raw.encode('utf-8')).decode('utf-8')
    
    return f"ssr://{ssr_base64}"

def convert_hysteria2(proxy):
    """转换Hysteria2节点"""
    server = proxy.get('server', '')
    port = proxy.get('port', '')
    password = proxy.get('password', '')
    name = urllib.parse.quote(proxy.get('name', ''))
    
    params = []
    if proxy.get('sni'):
        params.append(f"sni={proxy['sni']}")
    if proxy.get('skip-cert-verify'):
        params.append("insecure=1")
    
    query = '&'.join(params) if params else ''
    return f"hysteria2://{password}@{server}:{port}?{query}#{name}"

if __name__ == "__main__":
    asyncio.run(process_proxies())
