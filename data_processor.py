# data_processor.py
import fitdecode
import zipfile
import io
import json

class DataProcessor:
    def __init__(self):
        pass

    def parse_sleep_json(self, sleep_data):
        """Extracts total sleep seconds from Garmin JSON response."""
        if not sleep_data or 'dailySleepDTO' not in sleep_data:
            return 0
        
        # sleepTimeSeconds is often the key in Garmin's raw JSON
        return sleep_data['dailySleepDTO'].get('sleepTimeSeconds', 0)

    def decode_fit_file(self, file_path):
        """
        Decodes a .fit file to extract total distance (meters).
        Handles both raw .fit and zipped .fit files from Garmin.
        """
        distance_meters = 0.0
        
        try:
            # Check if it is a zip file (Garmin often sends .fit inside .zip)
            if zipfile.is_zipfile(file_path):
                with zipfile.ZipFile(file_path, 'r') as z:
                    # Assume the first file in zip is the fit file
                    fit_filename = z.namelist()[0]
                    with z.open(fit_filename) as f:
                        distance_meters = self._read_fit_stream(f)
            else:
                # Direct .fit file
                distance_meters = self._read_fit_stream(file_path)
                
        except Exception as e:
            print(f"Error decoding .fit file: {e}")
            
        return distance_meters

    def _read_fit_stream(self, source):
        """Internal helper to iterate through fit records."""
        total_dist = 0.0
        with fitdecode.FitReader(source) as fit_file:
            for frame in fit_file:
                if frame.frame_type == fitdecode.FIT_FRAME_DATA:
                    if frame.name == 'session':
                        if frame.has_field('total_distance'):
                            total_dist = frame.get_value('total_distance')
        return total_dist

    def format_duration(self, seconds):
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        return f"{hours}h {minutes}m"