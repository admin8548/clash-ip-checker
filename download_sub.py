import os
import requests
import yaml # 需要 PyYAML 库
import sys

# 1. 获取并分割链接
env_urls = os.environ.get("CLASH_SUB_URL", "")
# 使用 splitlines() 可以自动处理各种换行符，并过滤掉空行
urls = [url.strip() for url in env_urls.splitlines() if url.strip()]

if not urls:
    print("Error: No URLs found in CLASH_SUB_URL.")
    sys.exit(1)

print(f"Found {len(urls)} subscription links.")

# 用于存储合并后的所有节点
merged_proxies = []

headers = {
    "User-Agent": "Clash/1.0"
}

for index, url in enumerate(urls):
    print(f"[{index+1}/{len(urls)}] Downloading: {url[:15]}...")
    try:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        
        # 解析 YAML
        try:
            data = yaml.safe_load(resp.content)
        except yaml.YAMLError:
            print(f"  -> Warning: Failed to parse YAML from {url}. Skipping.")
            # 进阶提示：如果你的订阅是 Base64 编码的（不是 YAML），这里需要额外的解码逻辑
            continue

        # 提取 proxies 部分
        if data and 'proxies' in data and isinstance(data['proxies'], list):
            count = len(data['proxies'])
            print(f"  -> Success: Extracted {count} nodes.")
            merged_proxies.extend(data['proxies'])
        else:
            print(f"  -> Warning: No 'proxies' list found in {url}.")

    except Exception as e:
        print(f"  -> Error downloading {url}: {e}")

# 检查是否有节点
if not merged_proxies:
    print("No nodes extracted from any subscription. Exiting.")
    sys.exit(1)

# 3. 生成最终的合并文件
# 这是一个标准的 Clash 配置文件结构
final_config = {
    'proxies': merged_proxies
}

try:
    with open("config.yaml", "w", encoding='utf-8') as f:
        # allow_unicode=True 确保中文字符正常显示
        yaml.dump(final_config, f, allow_unicode=True, default_flow_style=False)
    
    print(f"All done! Merged {len(merged_proxies)} nodes into config.yaml")

except Exception as e:
    print(f"Failed to save file: {e}")
    sys.exit(1)
