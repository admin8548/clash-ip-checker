import os
import sys

# 立即输出诊断信息
print("🚀 download_sub.py starting...", flush=True)
print(f"Python version: {sys.version}", flush=True)
print(f"Working directory: {os.getcwd()}", flush=True)

try:
    import requests
    print("✅ requests imported", flush=True)
except ImportError as e:
    print(f"❌ Failed to import requests: {e}", flush=True)
    sys.exit(1)

try:
    import yaml
    print("✅ PyYAML imported", flush=True)
except ImportError as e:
    print(f"❌ Failed to import PyYAML: {e}", flush=True)
    sys.exit(1)

# 1. 获取并分割链接
print("📥 Reading CLASH_SUB_URL environment variable...", flush=True)
env_urls = os.environ.get("CLASH_SUB_URL", "")
print(f"   Raw env_urls length: {len(env_urls)} chars", flush=True)

# 使用 splitlines() 可以自动处理各种换行符，并过滤掉空行
urls = [url.strip() for url in env_urls.splitlines() if url.strip()]
print(f"   Parsed URLs count: {len(urls)}", flush=True)

if not urls:
    print("❌ Error: No URLs found in CLASH_SUB_URL.", flush=True)
    print(f"   env_urls content (first 100 chars): {repr(env_urls[:100])}", flush=True)
    sys.exit(1)

print(f"✅ Found {len(urls)} subscription links.", flush=True)

# 用于存储合并后的所有节点
merged_proxies = []

headers = {
    "User-Agent": "Clash/1.0"
}

for index, url in enumerate(urls):
    # 生成订阅源标识（Sub-1, Sub-2, ...）
    source_id = f"Sub-{index+1}"
    # 安全地显示URL（只显示前30个字符）
    safe_url = url[:30] + "..." if len(url) > 30 else url
    print(f"[{index+1}/{len(urls)}] Downloading: {safe_url} ({source_id})", flush=True)
    try:
        resp = requests.get(url, headers=headers, timeout=30)
        print(f"   Response status: {resp.status_code}", flush=True)
        resp.raise_for_status()
        
        # 显示响应内容类型和长度
        content_type = resp.headers.get('Content-Type', 'unknown')
        content_len = len(resp.content)
        print(f"   Content-Type: {content_type}, Length: {content_len} bytes", flush=True)
        
        # 解析 YAML
        try:
            data = yaml.safe_load(resp.content)
            print(f"   YAML parsed, type: {type(data).__name__}", flush=True)
        except yaml.YAMLError as ye:
            print(f"   ⚠️ Warning: Failed to parse YAML: {ye}", flush=True)
            # 显示内容前100字符用于调试
            print(f"   Content preview: {resp.content[:100]}", flush=True)
            continue

        # 提取 proxies 部分
        if data and 'proxies' in data and isinstance(data['proxies'], list):
            count = len(data['proxies'])
            print(f"   ✅ Success: Extracted {count} nodes from {source_id}.", flush=True)
            
            # 给每个节点添加订阅源标记
            for proxy in data['proxies']:
                if isinstance(proxy, dict):
                    proxy['_source'] = source_id
            
            merged_proxies.extend(data['proxies'])
        else:
            keys = list(data.keys()) if isinstance(data, dict) else "N/A"
            print(f"   ⚠️ Warning: No 'proxies' list found. Keys: {keys}", flush=True)

    except requests.exceptions.RequestException as e:
        print(f"   ❌ Request error: {e}", flush=True)
    except Exception as e:
        print(f"   ❌ Unexpected error: {type(e).__name__}: {e}", flush=True)

# 检查是否有节点
print(f"\n📊 Summary: Total merged proxies: {len(merged_proxies)}", flush=True)

if not merged_proxies:
    print("❌ No nodes extracted from any subscription. Exiting.", flush=True)
    sys.exit(1)

# 3. 生成最终的合并文件
# 这是一个标准的 Clash 配置文件结构
final_config = {
    'proxies': merged_proxies
}

print("💾 Writing config.yaml...", flush=True)
try:
    output_path = os.path.join(os.getcwd(), "config.yaml")
    with open(output_path, "w", encoding='utf-8') as f:
        # allow_unicode=True 确保中文字符正常显示
        yaml.dump(final_config, f, allow_unicode=True, default_flow_style=False)
    
    # 验证文件已写入
    if os.path.exists(output_path):
        file_size = os.path.getsize(output_path)
        print(f"✅ All done! Merged {len(merged_proxies)} nodes into config.yaml ({file_size} bytes)", flush=True)
    else:
        print(f"❌ Error: config.yaml was not created at {output_path}", flush=True)
        sys.exit(1)

except Exception as e:
    print(f"❌ Failed to save file: {type(e).__name__}: {e}", flush=True)
    import traceback
    traceback.print_exc()
    sys.exit(1)
