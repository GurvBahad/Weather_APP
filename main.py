import json
import math
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


CONFIG_PATH = Path(__file__).with_name("config.json")
WEATHER_CODES = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Rime fog", 51: "Light drizzle", 53: "Drizzle",
    55: "Heavy drizzle", 56: "Freezing drizzle", 57: "Heavy freezing drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain", 66: "Freezing rain",
    67: "Heavy freezing rain", 71: "Light snow", 73: "Snow", 75: "Heavy snow",
    77: "Snow grains", 80: "Rain showers", 81: "Rain showers",
    82: "Heavy rain showers", 85: "Snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm with hail", 99: "Thunderstorm with heavy hail",
}
COUNTRY_NEWS_LOCALE = {
    "FR": ("fr", "FR", "fr:fr"), "BE": ("fr", "BE", "BE:fr"),
    "CH": ("fr", "CH", "CH:fr"), "DE": ("de", "DE", "DE:de"),
    "AT": ("de", "AT", "AT:de"), "ES": ("es", "ES", "ES:es"),
    "MX": ("es", "MX", "MX:es"), "IT": ("it", "IT", "IT:it"),
    "PT": ("pt", "PT", "PT:pt"), "BR": ("pt", "BR", "BR:pt-419"),
    "NL": ("nl", "NL", "NL:nl"), "JP": ("ja", "JP", "JP:ja"),
}
NEARBY_OFFSETS = (
    ("North", 0.12, 0), ("South", -0.12, 0),
    ("East", 0, 0.12), ("West", 0, -0.12),
)
RAIN_CODES = {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99}
HOURLY_FIELDS = (
    "temperature_2m",
    "apparent_temperature",
    "precipitation_probability",
    "precipitation",
    "weather_code",
    "wind_speed_10m",
)


class WeatherError(Exception):
    """An error that can be returned to the browser."""


def fetch_json(url):
    """Fetch and decode a JSON response, translating network errors for the UI."""
    try:
        request = Request(url, headers={"User-Agent": "LocalWeatherApp/1.0"})
        with urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except URLError as error:
        raise WeatherError(f"Could not reach a weather service: {error.reason}") from error
    except TimeoutError as error:
        raise WeatherError("A weather service took too long to respond.") from error
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WeatherError("A weather service returned an unreadable response.") from error


def weather_url(latitude, longitude, include_hourly=False):
    """Build an Open-Meteo request for current conditions and optional forecasts."""
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "current": "temperature_2m,relative_humidity_2m,apparent_temperature,is_day,precipitation,weather_code,wind_speed_10m",
        "timezone": "auto",
    }
    if include_hourly:
        params.update({
            "hourly": "temperature_2m,apparent_temperature,precipitation_probability,precipitation,weather_code,wind_speed_10m",
            "forecast_hours": 24,
            "forecast_minutely_15": 5,
            "minutely_15": "precipitation,precipitation_probability",
        })
    return "https://api.open-meteo.com/v1/forecast?" + urlencode(params)


def rain_outlook(current, forecast):
    """Summarize precipitation estimates within the next hour."""
    try:
        if int(current["weather_code"]) in RAIN_CODES:
            return "Rain is currently reported."
        times = forecast["time"]
        amounts = forecast["precipitation"]
        probabilities = forecast["precipitation_probability"]
        now = datetime.fromisoformat(current["time"])
        if not times or len(times) != len(amounts) or len(times) != len(probabilities):
            return "Rain outlook unavailable."
        for time, amount, probability in zip(times, amounts, probabilities):
            minutes = int((datetime.fromisoformat(time) - now).total_seconds() // 60)
            # Use either measurable precipitation or a high model probability
            # so light rain remains visible even when the predicted amount rounds to zero.
            if 0 < minutes <= 60 and (float(amount) >= 0.1 or float(probability) >= 50):
                return f"Rain may start in about {minutes} minutes ({float(probability):.0f}% chance)."
        return "No rain forecast in the next 60 minutes."
    except (KeyError, TypeError, ValueError, OverflowError):
        return "Rain outlook unavailable."


def _weather_summary(current, units):
    code = int(current["weather_code"])
    return {
        "temperature": current["temperature_2m"],
        "feels_like": current["apparent_temperature"],
        "humidity": current["relative_humidity_2m"],
        "wind_speed": current["wind_speed_10m"],
        "precipitation": current["precipitation"],
        "condition": WEATHER_CODES.get(code, "Unknown conditions"),
        "weather_code": code,
        "is_day": bool(current["is_day"]),
        "temperature_unit": units.get("temperature_2m", "°C"),
        "wind_unit": units.get("wind_speed_10m", "km/h"),
        "time": current["time"],
    }


def _nearby_coordinates(latitude, longitude, latitude_offset, longitude_offset):
    # Longitude degrees narrow toward the poles; adjust the east/west distance
    # so each sample stays roughly comparable to the north/south offsets.
    longitude_scale = max(abs(math.cos(math.radians(latitude))), 0.35)
    adjusted_longitude_offset = longitude_offset / longitude_scale
    nearby_latitude = max(-89.5, min(89.5, latitude + latitude_offset))
    nearby_longitude = longitude + adjusted_longitude_offset
    return nearby_latitude, ((nearby_longitude + 180) % 360) - 180


def _nearby_weather(label, latitude, longitude):
    """Return current conditions for one nearby sample point."""
    data = fetch_json(weather_url(latitude, longitude))
    current = data.get("current")
    if not isinstance(current, dict):
        raise WeatherError("Nearby conditions are unavailable.")
    weather = _weather_summary(current, data.get("current_units", {}))
    return {
        "direction": label,
        "location": f"{label} of the selected location",
        "temperature": weather["temperature"],
        "temperature_unit": weather["temperature_unit"],
        "condition": weather["condition"],
        "weather_code": weather["weather_code"],
        "precipitation": weather["precipitation"],
    }


def _news_locale(country_code):
    """Return the Google News language, region, and edition for a country."""
    default_region = country_code or "US"
    return COUNTRY_NEWS_LOCALE.get(
        country_code, ("en", default_region, f"{default_region}:en")
    )


def get_weather_news(location, country_code):
    """Retrieve a small, location-specific set of weather headlines."""
    language, region, edition = _news_locale(country_code)
    query = urlencode({
        "q": f"weather {location}",
        "hl": language,
        "gl": region,
        "ceid": edition,
    })
    request = Request(
        "https://news.google.com/rss/search?" + query,
        headers={"User-Agent": "Mozilla/5.0 (compatible; LocalWeatherApp/1.0)"},
    )
    try:
        with urlopen(request, timeout=12) as response:
            document = ET.fromstring(response.read())
    except (URLError, TimeoutError, ET.ParseError, OSError) as error:
        raise WeatherError("The weather news feed is temporarily unavailable.") from error

    articles = []
    for item in document.findall("./channel/item")[:8]:
        title = item.findtext("title", "").strip()
        link = item.findtext("link", "").strip()
        if not title or not link or not link.startswith(("https://", "http://")):
            continue
        articles.append({
            "title": title,
            "url": link,
            "published": item.findtext("pubDate", "").strip(),
            "source": item.findtext("source", "").strip(),
        })
    return articles


def _fetch_overview_sources(location_name, country_code, latitude, longitude):
    """Fetch main, nearby, and news data concurrently; auxiliary feeds are optional."""
    jobs = [
        ("main", lambda: fetch_json(weather_url(latitude, longitude, include_hourly=True))),
        *[
            (
                label,
                lambda label=label, dlat=dlat, dlon=dlon: _nearby_weather(
                    label,
                    *_nearby_coordinates(latitude, longitude, dlat, dlon),
                ),
            )
            for label, dlat, dlon in NEARBY_OFFSETS
        ],
        ("news", lambda: get_weather_news(location_name, country_code)),
    ]
    results = {}
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = {pool.submit(job): key for key, job in jobs}
        for future, key in futures.items():
            try:
                results[key] = future.result()
            except WeatherError:
                if key == "main":
                    raise
                # News and nearby points are useful extras, but should not hide
                # the main forecast when one of their providers is unavailable.
                results[key] = None
    return results


def _build_hourly_forecast(data):
    """Validate Open-Meteo's parallel hourly arrays and return 24 display rows."""
    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    if not isinstance(times, list) or not times:
        raise WeatherError("The weather service did not return an hourly forecast.")
    if any(
        not isinstance(hourly.get(key), list) or len(hourly[key]) != len(times)
        for key in HOURLY_FIELDS
    ):
        raise WeatherError("The hourly forecast data is incomplete.")

    forecast = []
    for index, time in enumerate(times[:24]):
        code = int(hourly["weather_code"][index])
        forecast.append(
            {
                "time": time,
                "temperature": hourly["temperature_2m"][index],
                "feels_like": hourly["apparent_temperature"][index],
                "precipitation_probability": hourly["precipitation_probability"][index],
                "precipitation": hourly["precipitation"][index],
                "weather_code": code,
                "condition": WEATHER_CODES.get(code, "Unknown conditions"),
                "wind_speed": hourly["wind_speed_10m"][index],
            }
        )
    return forecast


def get_weather_overview(location):
    """Assemble weather data for a location into the browser API response."""
    location = location.strip()
    if not location:
        raise WeatherError("Enter a city or location.")
    if len(location) > 150:
        raise WeatherError("Location must be 150 characters or fewer.")

    geocoding_url = "https://geocoding-api.open-meteo.com/v1/search?" + urlencode(
        {"name": location, "count": 1, "language": "en", "format": "json"}
    )
    geocoding = fetch_json(geocoding_url)
    places = geocoding.get("results") if isinstance(geocoding, dict) else None
    if not places:
        raise WeatherError(f"No location found for “{location}”. Try a nearby city name.")
    place = places[0]
    try:
        latitude = float(place["latitude"])
        longitude = float(place["longitude"])
        name = place["name"]
    except (KeyError, TypeError, ValueError) as error:
        raise WeatherError("The location service returned incomplete details.") from error
    country_code = str(place.get("country_code", "")).upper()
    parts = [name]
    for part in (place.get("admin1"), place.get("country")):
        if part and part not in parts:
            parts.append(part)
    location_name = ", ".join(parts)

    results = _fetch_overview_sources(
        location_name, country_code, latitude, longitude
    )
    data = results["main"]
    current = data.get("current") if isinstance(data, dict) else None
    if not isinstance(current, dict):
        raise WeatherError("The weather service did not return current conditions.")
    current_weather = _weather_summary(current, data.get("current_units", {}))
    minutely = data.get("minutely_15", {})
    current_weather["rain_outlook"] = rain_outlook(current, minutely)
    return {
        "location": location_name,
        "country_code": country_code,
        "latitude": float(data.get("latitude", latitude)),
        "longitude": float(data.get("longitude", longitude)),
        "timezone": data.get("timezone", ""),
        "current": current_weather,
        "hourly": _build_hourly_forecast(data),
        "nearby": [results[key] for key, _, _ in NEARBY_OFFSETS if results.get(key)],
        "news": results.get("news") or [],
        "news_unavailable": results.get("news") is None,
    }


def load_last_location(config_path=CONFIG_PATH):
    """Read the most recently searched location, if a config file exists."""
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WeatherError(f"Could not read the saved location: {error}") from error
    if not isinstance(config, dict) or not isinstance(config.get("location", ""), str):
        raise WeatherError("The saved location file has an invalid format.")
    return config.get("location", "")


def save_last_location(location, config_path=CONFIG_PATH):
    """Persist the last successful search for the next app launch."""
    try:
        config_path.write_text(
            json.dumps({"location": location}, indent=2) + "\n", encoding="utf-8"
        )
    except OSError as error:
        raise WeatherError(f"Could not save the location: {error}") from error


APP_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#101b2c">
<title>Weather Nearby</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=Manrope:wght@400;600;700;800&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>
:root{color-scheme:dark;--bg:#0d1726;--panel:#152338;--panel2:#1a2b43;--text:#f2f6fc;--muted:#99abc3;--accent:#8bd5ff;--line:#263a54}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at 70% -10%,#244362 0,transparent 42%),var(--bg);color:var(--text);font:15px "DM Sans",sans-serif}main{max-width:1180px;margin:auto;padding:32px 22px 56px}
header{display:flex;justify-content:space-between;align-items:center;gap:20px;margin-bottom:32px}.brand{font:800 21px Manrope,sans-serif;letter-spacing:-.7px}.brand span{color:var(--accent)}.tag{color:var(--muted);font-size:13px}
.search{display:flex;gap:10px;width:min(100%,560px)}input{flex:1;min-width:0;background:#101d2e;border:1px solid #344a66;border-radius:12px;padding:14px 16px;color:var(--text);font:inherit;outline:none}input:focus{border-color:var(--accent)}button{border:0;border-radius:12px;background:var(--accent);color:#082035;padding:0 20px;font:700 14px "DM Sans";cursor:pointer}button:disabled{opacity:.55;cursor:wait}.status{min-height:26px;margin:12px 0 22px;color:var(--muted);font-size:13px}
.section-title{font:700 19px Manrope,sans-serif;letter-spacing:-.35px;margin:0 0 15px}.grid{display:grid;grid-template-columns:1.15fr .85fr;gap:16px}.card{background:linear-gradient(145deg,#192a41,#142237);border:1px solid var(--line);border-radius:18px;padding:22px;box-shadow:0 12px 35px #06101e44}
.current{min-height:230px;display:flex;flex-direction:column;justify-content:space-between}.location{font:700 20px Manrope}.local-time,.muted{color:var(--muted);font-size:13px}.temp{font:800 62px Manrope;letter-spacing:-4px;line-height:1}.condition{font-size:16px;margin-top:8px}.metrics{display:flex;gap:24px;flex-wrap:wrap;color:var(--muted);font-size:13px;margin-top:24px}.metrics b{display:block;color:var(--text);font-size:15px;margin-top:4px}
.rain{margin-top:16px;padding:12px 14px;border:1px solid #34516e;border-radius:12px;color:#c5eaff;background:#132b40}.map-card{padding:0;overflow:hidden}.map-head{padding:18px 20px 12px;display:flex;justify-content:space-between;align-items:center}.map-head h2{margin:0;font:700 17px Manrope}.map-head button{padding:9px 12px;font-size:12px}#map{height:320px;background:#1a293b}.leaflet-popup-content-wrapper,.leaflet-popup-tip{background:#152338;color:var(--text)}
.nearby,.hourly,.news{margin-top:26px}.near-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.near-item{padding:17px;background:var(--panel);border:1px solid var(--line);border-radius:15px}.near-item .direction{color:var(--muted);font-size:12px}.near-item .near-temp{font:700 25px Manrope;margin:10px 0 4px}.near-item .near-cond{font-size:13px}
.hours{display:flex;overflow-x:auto;gap:10px;padding-bottom:8px}.hour{min-width:112px;background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:14px 12px}.hour time{font-size:12px;color:var(--muted)}.hour .hour-temp{font:700 23px Manrope;margin:10px 0 2px}.hour .hour-cond{font-size:12px;min-height:33px}.chance{font-size:12px;color:#acdfff;margin-top:12px}.hour-summary{margin:13px 0 0;color:var(--muted);font-size:13px}
.news-list{display:grid;grid-template-columns:repeat(2,1fr);gap:12px}.article{display:block;padding:18px;background:var(--panel);border:1px solid var(--line);border-radius:15px;color:var(--text);text-decoration:none}.article:hover{border-color:var(--accent)}.article h3{margin:0 0 12px;font:600 15px/1.45 Manrope}.article span{font-size:12px;color:var(--muted)}.empty{color:var(--muted);padding:16px 0}
.hidden{display:none!important}.loading{opacity:.7}footer{margin-top:28px;color:var(--muted);font-size:12px}footer a{color:var(--accent)}
@media(max-width:760px){main{padding:22px 14px 40px}.grid{grid-template-columns:1fr}.near-grid{grid-template-columns:repeat(2,1fr)}.news-list{grid-template-columns:1fr}.tag{display:none}header{margin-bottom:23px}.temp{font-size:54px}.card{padding:18px}#map{height:280px}}
</style></head>
<body><main>
<header><div class="brand">WEATHER<span> / NEARBY</span></div><div class="tag">Current conditions · local outlook · weather news</div></header>
<form class="search" id="search"><input id="location" maxlength="150" placeholder="Search a city or location" autocomplete="off" required><button id="submit" type="submit">Get weather</button></form>
<div class="status" id="status">Enter a location to see local weather, nearby conditions, and the next 24 hours.</div>
<div id="results" class="hidden">
<section class="grid">
  <div class="card current">
    <div><div class="location" id="place"></div><div class="local-time" id="local-time"></div></div>
    <div><div class="temp" id="temperature"></div><div class="condition" id="condition"></div>
      <div class="metrics"><div>Feels like<b id="feels"></b></div><div>Humidity<b id="humidity"></b></div><div>Wind<b id="wind"></b></div><div>Precipitation<b id="precip"></b></div></div>
    </div><div class="rain" id="rain"></div>
  </div>
  <div class="card map-card"><div class="map-head"><h2>Nearby precipitation radar</h2><button id="refresh-radar" type="button">Refresh radar</button></div><div id="map"></div></div>
</section>
<section class="nearby"><h2 class="section-title">Around your location</h2><div class="near-grid" id="nearby"></div></section>
<section class="hourly"><h2 class="section-title">Next 24 hours</h2><div class="hours" id="hours"></div><p class="hour-summary" id="hour-summary"></p></section>
<section class="news"><h2 class="section-title">Weather news</h2><div class="news-list" id="news"></div><p class="muted">Weather-related news from Google News. Article links open the publisher's page.</p></section>
</div>
<footer>Weather forecasts by <a href="https://open-meteo.com/" target="_blank" rel="noreferrer">Open-Meteo</a> · Map © <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer">OpenStreetMap</a> · Radar by <a href="https://www.rainviewer.com/" target="_blank" rel="noreferrer">RainViewer</a></footer>
</main>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const $=id=>document.getElementById(id);
let map=null,marker=null,radar=null,radarTimer=null,weatherData=null;
function esc(s){const d=document.createElement("div");d.textContent=s??"";return d.innerHTML}
function fmt(n,d=0){return Number(n).toFixed(d)}
function icon(code){return ({0:"☀️",1:"🌤️",2:"⛅",3:"☁️",45:"🌫️",48:"🌫️",51:"🌦️",53:"🌦️",55:"🌧️",61:"🌧️",63:"🌧️",65:"🌧️",71:"🌨️",73:"🌨️",75:"❄️",80:"🌦️",81:"🌧️",82:"🌧️",85:"🌨️",86:"❄️",95:"⛈️",96:"⛈️",99:"⛈️"})[code]||"🌡️"}
function render(data){
 weatherData=data; $("results").classList.remove("hidden"); $("place").textContent=data.location;
 $("local-time").textContent="Local time · "+data.current.time.replace("T"," ")+" · "+(data.timezone||"");
 $("temperature").textContent=fmt(data.current.temperature,1)+"°"; $("condition").textContent=icon(data.current.weather_code)+"  "+data.current.condition;
 $("feels").textContent=fmt(data.current.feels_like,1)+"°"+data.current.temperature_unit.replace("°","");
 $("humidity").textContent=fmt(data.current.humidity)+"%"; $("wind").textContent=fmt(data.current.wind_speed)+" "+data.current.wind_unit;
 $("precip").textContent=fmt(data.current.precipitation,1)+" mm"; $("rain").textContent="Next 60 minutes: "+data.current.rain_outlook;
 $("nearby").innerHTML=data.nearby.length?data.nearby.map(n=>`<div class="near-item"><div class="direction">${esc(n.direction)} · nearby model point</div><div class="near-temp">${icon(n.weather_code)} ${fmt(n.temperature,1)}°${esc(n.temperature_unit.replace("°",""))}</div><div class="near-cond">${esc(n.condition)} · ${fmt(n.precipitation,1)} mm rain</div></div>`).join(""):'<div class="empty">Nearby conditions are unavailable.</div>';
 const hours=data.hourly||[];
 $("hours").innerHTML=hours.map(h=>`<div class="hour"><time>${esc(h.time.split("T")[1]?.slice(0,5)||h.time)}</time><div class="hour-temp">${icon(h.weather_code)} ${fmt(h.temperature,0)}°</div><div class="hour-cond">${esc(h.condition)}</div><div class="chance">☂ ${fmt(h.precipitation_probability)}% · ${fmt(h.precipitation,1)} mm</div></div>`).join("");
 const wet=hours.filter(h=>Number(h.precipitation)>=0.1||Number(h.precipitation_probability)>=50);
 $("hour-summary").textContent=wet.length?`Rain is possible in the next 24 hours; first chance around ${wet[0].time.split("T")[1]?.slice(0,5)||wet[0].time} (${fmt(wet[0].precipitation_probability)}% chance).`:"No significant rain is forecast in the next 24 hours.";
 // Escape RSS-provided text before placing it in HTML; article titles are external data.
 $("news").innerHTML=data.news.length?data.news.map(a=>`<a class="article" href="${esc(a.url)}" target="_blank" rel="noopener noreferrer"><h3>${esc(a.title)}</h3><span>${esc(a.source||"News")} · ${esc(a.published)}</span></a>`).join(""):'<div class="empty">No recent weather news found for this location.</div>';
 if(data.news_unavailable)$("news").innerHTML='<div class="empty">The weather news feed is temporarily unavailable.</div>';
 renderMap(data);
}
function renderMap(data){
 const center=[data.latitude,data.longitude];
 if(!window.L){$("map").textContent="The map library could not be loaded. Weather data is still available above.";return}
 if(!map){map=L.map("map").setView(center,9);L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png",{maxZoom:19,attribution:'&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'}).addTo(map)}
 else map.setView(center,9);
 if(marker)marker.remove();marker=L.marker(center).addTo(map).bindPopup(`<strong>${esc(data.location)}</strong><br>${esc(data.current.condition)} · ${fmt(data.current.temperature,1)}°`).openPopup();
 refreshRadar();if(radarTimer)clearInterval(radarTimer);radarTimer=setInterval(refreshRadar,300000);setTimeout(()=>map.invalidateSize(),100);
}
async function refreshRadar(){
 $("refresh-radar").disabled=true;
 try{const response=await fetch("https://api.rainviewer.com/public/weather-maps.json?nocache="+Date.now(),{cache:"no-store"});if(!response.ok)throw Error();const data=await response.json();const frames=data.radar?.past;if(!frames?.length)throw Error();const frame=frames[frames.length-1];if(radar)map.removeLayer(radar);radar=L.tileLayer(data.host+frame.path+"/256/{z}/{x}/{y}/2/1_1.png",{maxNativeZoom:7,maxZoom:19,opacity:.65,attribution:'<a href="https://www.rainviewer.com/">RainViewer radar</a>'}).addTo(map);$("refresh-radar").textContent="Radar · "+new Date(frame.time*1000).toLocaleTimeString([],{hour:"numeric",minute:"2-digit"})}
 catch{$("refresh-radar").textContent="Radar unavailable · retry"}
 finally{$("refresh-radar").disabled=false}
}
async function search(location){
 $("submit").disabled=true;$("status").textContent="Loading current and nearby weather, 24-hour forecast, and news…";$("status").classList.add("loading");
 try{const response=await fetch("/api/weather",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({location})});const data=await response.json();if(!response.ok)throw Error(data.error||"Could not load weather.");$("location").value=location;render(data);$("status").textContent="Weather updated for "+data.location+".";localStorage.setItem("weather-location",location)}
 catch(error){$("status").textContent=error.message}
 finally{$("submit").disabled=false;$("status").classList.remove("loading")}
}
$("search").addEventListener("submit",event=>{event.preventDefault();search($("location").value.trim())});
$("refresh-radar").addEventListener("click",refreshRadar);
fetch("/api/config").then(r=>r.json()).then(data=>{const saved=localStorage.getItem("weather-location")||data.location;if(saved){$("location").value=saved;search(saved)}}).catch(()=>{});
</script></body></html>"""


def create_server(host="127.0.0.1", port=8000):
    """Create the local HTTP server for the dashboard and its small JSON API."""
    class RequestHandler(BaseHTTPRequestHandler):
        def _send_json(self, status, data):
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/":
                body = APP_HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/api/config":
                try:
                    location = load_last_location()
                except WeatherError as error:
                    self._send_json(500, {"error": str(error)})
                else:
                    self._send_json(200, {"location": location})
            else:
                self._send_json(404, {"error": "Not found."})

        def do_POST(self):
            if self.path != "/api/weather":
                self._send_json(404, {"error": "Not found."})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 4096:
                    raise WeatherError("Invalid request size.")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(payload, dict) or not isinstance(payload.get("location"), str):
                    raise WeatherError("Enter a valid location.")
                location = payload["location"].strip()
                result = get_weather_overview(location)
                save_last_location(location)
            except WeatherError as error:
                self._send_json(400, {"error": str(error)})
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                self._send_json(400, {"error": "Invalid request. Enter a location and try again."})
            else:
                self._send_json(200, result)

        def log_message(self, _format, *_args):
            pass

    server = ThreadingHTTPServer((host, port), RequestHandler)
    server.daemon_threads = True
    return server


def main():
    import webbrowser

    server = create_server()
    url = f"http://{server.server_address[0]}:{server.server_port}/"
    print(f"Weather app is running at {url}")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
