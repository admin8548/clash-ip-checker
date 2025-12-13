import asyncio
import yaml
import aiohttp
import urllib.parse
import os
import sys
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
    
    # 创建临时checker用于快速IP检测
    temp_checker = IPChecker(headless=True)
    await temp_checker.start()
    
    try:
        for i, proxy in enumerate(valid_proxies):
            name = proxy['name']
            print(f"   [{i+1}/{len(valid_proxies)}] Checking: {name}")
            
            # 切换节点
            if not await controller.switch_proxy(selector_to_use, name):
                print(f"      -> Switch failed, keeping node.")
                unique_proxies.append(proxy)
                continue

            await asyncio.sleep(1)  # 等待切换生效
            
            # 快速获取IP
            ip = await temp_checker.get_simple_ip(local_proxy_url)
            
            if ip:
                if ip not in ip_to_proxy:
                    ip_to_proxy[ip] = proxy
                    unique_proxies.append(proxy)
                    print(f"      ✅ {ip} | {name}")
                else:
                    print(f"      ⏭️ {ip} | {name} (duplicate of {ip_to_proxy[ip]['name']})")
            else:
                # IP获取失败的也保留,后续浏览器检测
                unique_proxies.append(proxy)
                print(f"      ❓ Unknown IP | {name}")
    finally:
        await temp_checker.stop()
    
    print(f"\n📊 [Phase 1.5 Summary] Unique IPs: {len(unique_proxies)} / {len(valid_proxies)}")
    
    # --- 阶段 2: IP 纯净度检查 (原有逻辑) ---
    print(f"\n🕵️ [Phase 2] Starting IP Purity Check for {len(unique_proxies)} nodes...")

    checker = IPChecker(headless=True)
    await checker.start()

    results_map = {} # name -> result_suffix

    try:
        for i, proxy in enumerate(unique_proxies):
            name = proxy['name']
            print(f"\n[{i+1}/{len(unique_proxies)}] Checking: {name}")
            
            # 切换节点
            if not await controller.switch_proxy(selector_to_use, name):
                print("  -> Switch failed.")
                continue

            await asyncio.sleep(2) # 等待切换生效

            # 测 IP
            res = None
            for attempt in range(2):
                try:
                    res = await checker.check(proxy=local_proxy_url)
                    if res.get('error') is None and res.get('pure_score') != '❓':
                         break
                    if attempt == 0:
                        await asyncio.sleep(2)
                except Exception:
                     pass
            
            if not res:
                 res = {"full_string": "【❌ Error】", "ip": "Error"}

            full_str = res['full_string']
            print(f"  -> Result: {full_str} | IP: {res.get('ip')}")
            results_map[name] = full_str

    except KeyboardInterrupt:
        print("\nInterrupted. Saving...")
    finally:
        await checker.stop()

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
            # 加上检测结果后缀
            new_name = f"{old_name} {results_map[old_name]}"
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
        print(f"\nSuccess! Saved {len(final_proxies)} nodes to: {output_path}")
    except Exception as e:
        print(f"Error saving config: {e}")

if __name__ == "__main__":
    asyncio.run(process_proxies())
