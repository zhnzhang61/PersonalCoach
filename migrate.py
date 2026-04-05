import os

from garmin_ticket_login import migrate_pirate_token_to_garth


def migrate_token():
    pirate_path = os.path.expanduser("~/.local/share/pirate-garmin/native-oauth2.json")
    if not os.path.exists(pirate_path):
        print("❌ 找不到 pirate-garmin 的 token 文件。")
        return
    migrate_pirate_token_to_garth(pirate_path)


if __name__ == "__main__":
    migrate_token()
