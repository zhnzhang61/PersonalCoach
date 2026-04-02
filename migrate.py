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