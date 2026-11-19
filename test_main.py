import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from urllib.request import Request, urlopen
from unittest.mock import patch

from main import (
    APP_HTML,
    WeatherError,
    create_server,
    get_weather_news,
    get_weather_overview,
    load_last_location,
    _nearby_coordinates,
    rain_outlook,
    save_last_location,
)


class WeatherAppTests(unittest.TestCase):
    def test_location_is_saved_and_loaded(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            save_last_location("Paris, France", config_path)
            self.assertEqual(load_last_location(config_path), "Paris, France")
            self.assertEqual(
                json.loads(config_path.read_text(encoding="utf-8")),
                {"location": "Paris, France"},
            )

    def test_missing_config_returns_empty_location(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(load_last_location(Path(directory) / "missing.json"), "")

    def test_nearby_coordinates_stay_in_valid_ranges(self):
        latitude, longitude = _nearby_coordinates(89.9, 179.9, 0.12, 0.12)
        self.assertLessEqual(latitude, 89.5)
        self.assertGreaterEqual(longitude, -180)
        self.assertLess(longitude, 180)

    def test_weather_overview_returns_current_nearby_hourly_and_news(self):
        now = datetime.now().replace(minute=0, second=0, microsecond=0)
        hourly = {
            "time": [(now + timedelta(hours=i)).isoformat() for i in range(24)],
            "temperature_2m": [12.5] * 24,
            "apparent_temperature": [11.0] * 24,
            "precipitation_probability": [0] * 23 + [70],
            "precipitation": [0] * 23 + [0.2],
            "weather_code": [2] * 24,
            "wind_speed_10m": [8] * 24,
        }
        weather_data = {
            "current": {
                "time": now.isoformat(),
                "temperature_2m": 14.2,
                "apparent_temperature": 13.1,
                "relative_humidity_2m": 62,
                "wind_speed_10m": 9.5,
                "precipitation": 0.0,
                "weather_code": 2,
                "is_day": 1,
            },
            "current_units": {"temperature_2m": "°C", "wind_speed_10m": "km/h"},
            "latitude": 48.8566,
            "longitude": 2.3522,
            "timezone": "Europe/Paris",
            "hourly": hourly,
            "minutely_15": {
                "time": [now.isoformat()],
                "precipitation": [0],
                "precipitation_probability": [0],
            },
        }
        geocoding = {
            "results": [{
                "name": "Paris",
                "admin1": "Île-de-France",
                "country": "France",
                "country_code": "FR",
                "latitude": 48.8566,
                "longitude": 2.3522,
            }]
        }

        def fake_fetch(url):
            return geocoding if "geocoding-api" in url else weather_data

        with patch("main.fetch_json", side_effect=fake_fetch), patch(
            "main.get_weather_news",
            return_value=[{"title": "Local weather alert", "url": "https://example.com"}],
        ):
            result = get_weather_overview("Paris")

        self.assertEqual(result["location"], "Paris, Île-de-France, France")
        self.assertEqual(result["current"]["temperature"], 14.2)
        self.assertEqual(result["timezone"], "Europe/Paris")
        self.assertEqual(len(result["hourly"]), 24)
        self.assertEqual(result["hourly"][-1]["precipitation_probability"], 70)
        self.assertEqual(len(result["nearby"]), 4)
        self.assertEqual(result["news"][0]["title"], "Local weather alert")

    def test_invalid_or_unknown_location_is_rejected(self):
        with self.assertRaisesRegex(WeatherError, "Enter a city"):
            get_weather_overview(" ")
        with patch("main.fetch_json", return_value={"results": []}):
            with self.assertRaisesRegex(WeatherError, "No location found"):
                get_weather_overview("Nowhere")

    def test_rain_outlook_reports_rain_already_occurring(self):
        self.assertEqual(
            rain_outlook({"time": "2026-11-19T12:00", "weather_code": 61}, {}),
            "Rain is currently reported.",
        )

    def test_rain_outlook_reports_rain_starting_soon(self):
        current = {"time": "2026-11-19T12:00", "weather_code": 0}
        forecast = {
            "time": ["2026-11-19T12:00", "2026-11-19T12:15"],
            "precipitation": [0, 0.2],
            "precipitation_probability": [0, 70],
        }
        self.assertEqual(
            rain_outlook(current, forecast),
            "Rain may start in about 15 minutes (70% chance).",
        )

    def test_news_feed_parses_and_validates_article_links(self):
        rss = b"""<rss><channel><item><title>Weather alert</title>
        <link>https://news.example/article</link><pubDate>Today</pubDate>
        <source>Example News</source></item>
        <item><title>Bad link</title><link>javascript:alert(1)</link></item>
        </channel></rss>"""
        with patch("main.urlopen", return_value=FakeResponse(rss)):
            articles = get_weather_news("Paris, France", "FR")
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0]["title"], "Weather alert")
        self.assertEqual(articles[0]["source"], "Example News")

    def test_browser_page_contains_weather_dashboard_sections(self):
        self.assertIn('id="nearby"', APP_HTML)
        self.assertIn('id="hours"', APP_HTML)
        self.assertIn('id="news"', APP_HTML)
        self.assertIn("RainViewer", APP_HTML)
        self.assertNotIn("speak_weather_summary", APP_HTML)

    def test_server_serves_browser_page_and_api_config(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            save_last_location("Paris", config_path)
            server = create_server(port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                root = f"http://127.0.0.1:{server.server_port}"
                with urlopen(root + "/") as response:
                    self.assertIn(b"Weather Nearby", response.read())
                with patch("main.load_last_location", return_value="Paris"):
                    with urlopen(root + "/api/config") as response:
                        self.assertEqual(json.loads(response.read()), {"location": "Paris"})
                request = Request(
                    root + "/api/weather",
                    data=json.dumps({"location": "Paris"}).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with patch("main.get_weather_overview", return_value={"location": "Paris"}), patch(
                    "main.save_last_location"
                ):
                    with urlopen(request) as response:
                        self.assertEqual(json.loads(response.read()), {"location": "Paris"})
                bad_request = Request(
                    root + "/api/weather",
                    data=b'{"location": ""}',
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with patch("main.get_weather_overview", side_effect=WeatherError("Enter a city.")):
                    with self.assertRaises(Exception) as error:
                        urlopen(bad_request)
                    self.assertEqual(error.exception.code, 400)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


class FakeResponse(BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


if __name__ == "__main__":
    unittest.main()
