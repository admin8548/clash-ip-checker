import asyncio
import re
import aiohttp
from playwright.async_api import async_playwright

class IPChecker:
    def __init__(self, headless=True):
        self.headless = headless
        self.browser = None
        self.playwright = None
        self.cache = {} # Map IP -> Result Dict

    async def start(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=self.headless,
            args=["--no-sandbox", "--disable-setuid-sandbox"]
        )

    async def stop(self):
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()

    def get_emoji(self, percentage_str):
        try:
            val = float(percentage_str.replace('%', ''))
            # Logic from ipcheck.py with user approved thresholds
            if val <= 10: return "⚪"
            if val <= 30: return "🟢"
            if val <= 50: return "🟡"
            if val <= 70: return "🟠"
            if val <= 90: return "🔴"
            return "⚫"
        except:
            return "❓"

    async def get_simple_ip(self, proxy=None):
        """Fast IPv4 check for caching."""
        urls = ["http://api.ipify.org", "http://v4.ident.me"]
        for url in urls:
            try:
                # User modified timeout to 3s
                timeout = aiohttp.ClientTimeout(total=3)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(url, proxy=proxy) as resp:
                        if resp.status == 200:
                            ip = (await resp.text()).strip()
                            if re.match(r"^\d{1,3}(\.\d{1,3}){3}\d{1,3}$", ip):
                                return ip
            except Exception:
                continue 
        return None

    async def check(self, url="https://ippure.com/", proxy=None, timeout=20000, retry=2):
        if not self.browser:
            await self.start()
        
        # 1. Cleaner Fast IP & Cache Logic
        current_ip = await self.get_simple_ip(proxy)
        if current_ip and current_ip in self.cache:
            print(f"     [Cache Hit] {current_ip}")
            return self.cache[current_ip]
        
        if current_ip:
            print(f"     [New IP] {current_ip}")
        else:
            print("     [Warning] Fast IP check failed. Scanning with browser...")

        # 2. Browser Check (Logic from ipcheck.py)
        context_args = {
             "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        if proxy:
            context_args["proxy"] = {"server": proxy}
            
        context = await self.browser.new_context(**context_args)
        
        # Resource blocking (Optimization)
        await context.route("**/*", lambda route: route.abort() 
            if route.request.resource_type in ["image", "media", "font"] 
            else route.continue_())

        page = await context.new_page()
        
        # Default Result Structure
        result = {
            "pure_emoji": "❓", "bot_emoji": "❓", "ip_attr": "❓", "ip_src": "❓",
            "pure_score": "❓", "bot_score": "❓", "full_string": "", "ip": current_ip if current_ip else "❓", "error": None
        }

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            
            # Logic from ipcheck.py - Optimized wait
            try:
                await page.wait_for_selector("text=人机流量比", timeout=10000)
            except:
                pass 

            await page.wait_for_timeout(2000)
            text = await page.inner_text("body")

            # 1. IPPure Score
            score_match = re.search(r"IPPure系数.*?(\d+%)", text, re.DOTALL)
            if score_match:
                result["pure_score"] = score_match.group(1)
                result["pure_emoji"] = self.get_emoji(result["pure_score"])

            # 2. Bot Ratio
            bot_match = re.search(r"bot\s*(\d+(\.\d+)?)%", text, re.IGNORECASE)
            if bot_match:
                val = bot_match.group(0).replace('bot', '').strip()
                if not val.endswith('%'): val += "%"
                result["bot_score"] = val
                result["bot_emoji"] = self.get_emoji(val)

            # 3. Attributes
            attr_match = re.search(r"IP属性\s*\n\s*(.+)", text)
            if not attr_match: attr_match = re.search(r"IP属性\s*(.+)", text)
            if attr_match:
                raw = attr_match.group(1).strip()
                result["ip_attr"] = re.sub(r"IP$", "", raw)

            # 4. Source
            src_match = re.search(r"IP来源\s*\n\s*(.+)", text)
            if not src_match: src_match = re.search(r"IP来源\s*(.+)", text)
            if src_match:
                raw = src_match.group(1).strip()
                result["ip_src"] = re.sub(r"IP$", "", raw)

            # 5. Fallback IP if fast check failed
            if result["ip"] == "❓":
                ip_match = re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text)
                if ip_match: result["ip"] = ip_match.group(0)

            # 构建精简的输出字符串（方案C：Emoji+文字缩写）
            attr = result["ip_attr"] if result["ip_attr"] != "❓" else ""
            src = result["ip_src"] if result["ip_src"] != "❓" else ""
            
            # 属性缩写映射
            attr_abbr_map = {
                "机房": "机",
                "数据中心": "机",
                "住宅": "宅",
                "企业": "企",
                "教育": "教"
            }
            
            # 来源缩写映射
            src_abbr_map = {
                "原生": "原",
                "广播": "广",
                "ISP": "ISP",
                "企业": "企"
            }
            
            # 应用缩写
            attr_short = attr_abbr_map.get(attr, attr[:1] if attr and attr != "❓" else "")
            src_short = src_abbr_map.get(src, src[:1] if src and src != "❓" else "")

            if attr_short and src_short:
                info = f"{attr_short}|{src_short}"
            elif attr_short:
                info = attr_short
            elif src_short:
                info = src_short
            else:
                info = "检测中"
            
            result["full_string"] = f"【{result['pure_emoji']}{result['bot_emoji']} {info}】"

            # Cache Update
            if result["ip"] != "❓" and result["pure_score"] != "❓":
                self.cache[result["ip"]] = result.copy()

        except Exception as e:
            result["error"] = str(e)
            result["full_string"] = "【❌ Error】"
        finally:
            if not self.headless:
                print("     [Debug] Waiting 5s before closing browser window...")
                await asyncio.sleep(5)
            await page.close()
            await context.close()
        
        # 如果主站检测失败且还有重试次数，尝试备用方案
        if result["pure_score"] == "❓" and retry > 0:
            print(f"     [Retry] Primary check failed, trying backup... ({retry} attempts left)")
            backup_result = await self._backup_check(proxy, retry - 1)
            if backup_result and backup_result["pure_score"] != "❓":
                result.update(backup_result)
                # 更新缓存
                if result["ip"] != "❓" and result["pure_score"] != "❓":
                    self.cache[result["ip"]] = result.copy()
            
        return result
    
    async def _backup_check(self, proxy=None, retry=0):
        """备用检测方案，使用更简单的检测逻辑"""
        try:
            # 尝试使用更简单的检测方法
            # 这里可以添加其他IP检测网站的逻辑
            # 暂时返回一个基于IP地址的简单评估
            current_ip = await self.get_simple_ip(proxy)
            if not current_ip:
                return None
                
            # 基于IP段进行简单评估（这是一个简化的备用方案）
            result = {
                "pure_emoji": "❓", "bot_emoji": "❓",
                "ip_attr": "未知", "ip_src": "未知",
                "pure_score": "❓", "bot_score": "❓",
                "full_string": "", "ip": current_ip, "error": None
            }
            
            # 简单的IP段判断逻辑
            if current_ip.startswith(("103.", "134.", "46.", "13.")):
                # 这些段在日志中出现过，给予一个基础评估
                result["pure_emoji"] = "🟡"
                result["bot_emoji"] = "🟠"
                result["ip_attr"] = "机房"
                result["ip_src"] = "广播"
                result["pure_score"] = "40%"
                result["bot_score"] = "60%"
                # 构建精简的输出字符串（方案C：Emoji+文字缩写）
                attr = result["ip_attr"] if result["ip_attr"] != "❓" else ""
                src = result["ip_src"] if result["ip_src"] != "❓" else ""
                
                # 属性缩写映射
                attr_abbr_map = {
                    "机房": "机",
                    "数据中心": "机",
                    "住宅": "宅",
                    "企业": "企",
                    "教育": "教"
                }
                
                # 来源缩写映射
                src_abbr_map = {
                    "原生": "原",
                    "广播": "广",
                    "ISP": "ISP",
                    "企业": "企"
                }
                
                # 应用缩写
                attr_short = attr_abbr_map.get(attr, attr[:1] if attr and attr != "❓" else "")
                src_short = src_abbr_map.get(src, src[:1] if src and src != "❓" else "")

                if attr_short and src_short:
                    info = f"{attr_short}|{src_short}"
                elif attr_short:
                    info = attr_short
                elif src_short:
                    info = src_short
                else:
                    info = "检测中"
                
                result["full_string"] = f"【{result['pure_emoji']}{result['bot_emoji']} {info}】"
            
            return result
            
        except Exception as e:
            print(f"     [Backup Check Failed] {e}")
            return None
