## Garmin Error 429

由于 Garmin 目前全面启用了严格的 Cloudflare 防爬虫机制，使用 Playwright 等自动化浏览器登录极易触发 `HTTP 429 Too Many Requests` 和无限验证码死循环。

本方案通过**浏览器手动抓包获取一次性票据 (Service Ticket)**，并在代码中光速完成兑换，最终提取出原生 `garminconnect` 可用的长效 `OAuth2` 通行证。

### 步骤 1：手动获取一次性服务票据 (Service Ticket)
> ⚠️ **注意**：Service Ticket (`ST-xxxx`) 是一次性的，且有效期极短（不到 1 分钟），获取后必须立刻使用。

1. 打开一个**全新/无痕模式**的浏览器窗口。
2. 按 `F12` 打开开发者工具，切换到 **Network（网络）** 面板。
3. **关键操作**：在 Network 面板顶部勾选 **Preserve log（保留日志）**。
4. 在地址栏输入并访问以下专属移动端登录链接：
   ```text
   [https://sso.garmin.com/mobile/sso/en_US/sign-in?clientId=GCM_ANDROID_DARK&service=https://mobile.integration.garmin.com/gcm/android](https://sso.garmin.com/mobile/sso/en_US/sign-in?clientId=GCM_ANDROID_DARK&service=https://mobile.integration.garmin.com/gcm/android)
   ```
5. 正常输入账号密码登录（如有真人验证码则手动通过）。
6. 登录成功后，页面会跳转并显示找不到网页 (`This site can't be reached`)，**这是正常现象**。
7. 立刻查看浏览器地址栏，复制 `ticket=` 后面的完整字符串，这就是你的服务票据：
   ```text
   ST-xxxxxxx-xxxxxxxxxxxxxx-sso
   ```

### 步骤 2：魔改 `pirate-garmin` 拦截登录流程
原生 `pirate-garmin` 强制使用无头浏览器登录，我们需要修改源码让它接收手动传入的 Ticket。

1. 找到本地环境中的 `pirate_garmin/auth.py` 文件（例如在 `.venv/lib/python3.x/site-packages/pirate_garmin/auth.py`）。
2. 搜索 `def create_native_session` 方法。
3. 将该方法内尝试调用浏览器登录的代码（`login_via_browser` 相关段落）替换为手动输入逻辑：
   ```python
   def create_native_session(self) -> NativeOAuth2Session:
       # 强行注入手动抓取的 Service Ticket
       manual_ticket = input("\n[+] 请粘贴最新的 ST-xxxx 票据并回车: ").strip()
       
       with httpx.Client(follow_redirects=True, timeout=self.timeout) as client:
           di_slot = self.exchange_service_ticket_for_di_token(
               client, manual_ticket, DI_CLIENT_IDS
           )
       # ... 保留后续原有的 it_slot 代码 ...
   ```
4. 运行 `uv run pirate-garmin login`，此时终端会暂停等待。
5. **拼手速**：回到浏览器重新走一遍“步骤 1”拿一个新鲜的 Ticket，立刻粘贴到终端并回车。
6. 成功后，长效 Token 会保存在 `~/.local/share/pirate-garmin/native-oauth2.json`。此时可将 `auth.py` 代码恢复原状。

### 步骤 3：将 Token 迁移给原生 `garminconnect` (Garth)
`garminconnect` (底层基于 `garth`) 需要将 Token 放在 `~/.garth` 目录下。运行以下 Python 脚本完成“偷梁换柱”：

```python
import json
import os

def migrate_token():
    # 读取 pirate-garmin 抓到的新架构 Token
    pirate_path = os.path.expanduser("~/.local/share/pirate-garmin/native-oauth2.json")
    if not os.path.exists(pirate_path):
        print("❌ 找不到 pirate-garmin 的 token 文件。")
        return

    with open(pirate_path, "r") as f:
        pirate_data = json.load(f)

    # 提取核心的 DI OAuth2 Token
    oauth2_token = pirate_data["di"]["token"]

    # 存入 Garth 目录
    garth_dir = os.path.expanduser("~/.garth")
    os.makedirs(garth_dir, exist_ok=True)
    
    with open(os.path.join(garth_dir, "oauth2_token.json"), "w") as f:
        json.dump(oauth2_token, f, indent=4)
        
    print("✅ 长效通行证已植入 ~/.garth/oauth2_token.json")

if __name__ == "__main__":
    migrate_token()
```

### 步骤 4：绕过旧版 OAuth1 检查（兼容性补丁）
如果你的 `garminconnect` 库依然报错找不到 `oauth1_token.json`，这是因为旧版代码仍保留着双轨制检查。生成以下空壳文件“骗”过检查即可免密登录：

```python
import json
import os

garth_dir = os.path.expanduser("~/.garth")

# 生成假的 OAuth1 票据
with open(os.path.join(garth_dir, "oauth1_token.json"), "w") as f:
    json.dump({"oauth_token": "dummy", "oauth_token_secret": "dummy"}, f)

# 生成空的 domain profile
with open(os.path.join(garth_dir, "domain_profile.json"), "w") as f:
    json.dump({}, f)

print("✅ 兼容性假文件已生成！你的脚本现在应该可以满血运行了。")
```