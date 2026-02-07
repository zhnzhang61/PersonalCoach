# garmin_sync.py
import os
import datetime
from garminconnect import Garmin
from garminconnect import GarminConnectConnectionError, GarminConnectAuthenticationError

class GarminDownloader:
    def __init__(self, email, password):
        self.email = email
        self.password = password
        self.client = None

    def connect(self):
        try:
            self.client = Garmin(self.email, self.password)
            self.client.login()
            print("Login successful.")
        except (GarminConnectConnectionError, GarminConnectAuthenticationError, Exception) as err:
            print(f"Error occurred during Garmin login: {err}")
            return False
        return True

    def get_latest_sleep_data(self):
        """Fetches sleep data for 'last night' (today's date relative to wake up)."""
        if not self.client: return None
        today = datetime.date.today().isoformat()
        return self.client.get_sleep_data(today)

    def download_latest_run_fit(self, download_dir='data'):
        """Finds latest run, downloads .fit file."""
        if not self.client: return None
        
        # Get last 10 activities to find a run
        activities = self.client.get_activities(0, 10)
        if not os.path.exists(download_dir):
            os.makedirs(download_dir)

        for activity in activities:
            activity_type = activity['activityType']['typeKey']
            if activity_type in ['running', 'swimming']:
                activity_id = activity['activityId']
                print(f"Found {activity_type} (ID: {activity_id}). Downloading .fit...")
                
                # Download .fit file
                zip_data = self.client.download_activity(activity_id, dl_fmt=self.client.ActivityDownloadFormat.ORIGINAL)
                
                # Note: The API returns a zip containing the .fit. 
                # For simplicity here, we save the zip/bytes directly as .fit for the processor to handle 
                # or unzip. (Standard garminconnect library download usually requires unzip).
                # Saving as raw bytes for the processor.
                file_path = os.path.join(download_dir, f"{activity_id}.fit")
                with open(file_path, "wb") as fb:
                    fb.write(zip_data)
                
                return file_path
        return None

if __name__ == "__main__":
    # Test execution
    email = os.getenv("GARMIN_EMAIL")
    password = os.getenv("GARMIN_PASS")
    if email and password:
        downloader = GarminDownloader(email, password)
        if downloader.connect():
            downloader.download_latest_run_fit()