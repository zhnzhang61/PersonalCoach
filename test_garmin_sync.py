import unittest
import os
import shutil
import json
from unittest.mock import MagicMock, patch, mock_open, ANY
from garmin_sync import GarminSync

# --- Mock Client ---
class MockGarminClient:
    def login(self):
        pass
    
    # 1. Static Method
    def get_device_settings(self):
        return {"device": "fenix"}

    # 2. Daily Method
    def get_user_summary(self, cdate):
        return {"steps": 1000}

    # 3. Range Method (Fixed Mock Data)
    def get_activities(self, start, limit):
        # Added 'activityType' to prevent the "Error in Activity Phase" message
        return [{
            "activityId": 123, 
            "activityType": {"typeKey": "running"}, 
            "startTimeLocal": "2023-01-01 12:00:00"
        }]

    def download_activity(self, activity_id, dl_fmt=None):
        return b"fake_zip_data"

class TestGarminSync(unittest.TestCase):

    def setUp(self):
        self.test_dir = "test_data_temp"
        self.syncer = GarminSync("test@test.com", "pass", data_dir=self.test_dir)
        self.syncer.client = MockGarminClient()

    def tearDown(self):
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)

    def test_init_creates_directories(self):
        """Ensure the basic data folder is created upon initialization."""
        self.assertTrue(os.path.exists(self.test_dir))

    def test_introspect_api_categorization(self):
        """Verify dynamic discovery of methods."""
        self.syncer._introspect_api()
        self.assertIn('get_user_summary', self.syncer.daily_methods)
        self.assertIn('get_device_settings', self.syncer.static_methods)
        self.assertIn('get_activities', self.syncer.range_methods)

    @patch("builtins.open", new_callable=mock_open)
    @patch("json.dump")
    @patch("os.path.exists")
    def test_run_comprehensive_sync_saves_data(self, mock_exists, mock_json_dump, mock_file_open):
        """
        Verify that data is saved. 
        We use assert_any_call because multiple saves happen in sequence.
        """
        # Setup: Pretend files don't exist so it triggers downloads
        mock_exists.return_value = False 
        
        # Limit scope to ensure our target method runs
        self.syncer.daily_methods = ['get_user_summary'] 
        
        # Execute
        self.syncer.run_comprehensive_sync(days_back=1, activity_limit=1)

        # Verification: Check if get_user_summary data was saved at any point
        # ANY allows us to ignore the file handler argument
        mock_json_dump.assert_any_call({"steps": 1000}, ANY, indent=4, ensure_ascii=False)
        
        # Verification: Check if activity list was saved
        # Note: The mock returns the list, so we check for that list
        expected_activity_list = [{
            "activityId": 123, 
            "activityType": {"typeKey": "running"}, 
            "startTimeLocal": "2023-01-01 12:00:00"
        }]
        mock_json_dump.assert_any_call(expected_activity_list, ANY, indent=4, ensure_ascii=False)

if __name__ == '__main__':
    unittest.main()