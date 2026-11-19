# Weather Nearby

A small, local web app for current conditions, nearby weather, an interactive
precipitation map, location-related weather news, and an hourly forecast for the
next 24 hours. The last successfully searched location is saved in `config.json`
beside `main.py`.

## Run

From PowerShell in the project folder:

```powershell
python main.py
```

The app starts a local server at `http://127.0.0.1:8000/` and opens it in your
browser. Press `Ctrl+C` in the terminal to stop it. No extra Python packages or
API keys are required.

The app needs an internet connection. Forecasts and nearby conditions come from
[Open-Meteo](https://open-meteo.com/); local weather headlines come from Google
News RSS; the map uses [OpenStreetMap](https://www.openstreetmap.org/copyright)
and [RainViewer radar](https://www.rainviewer.com/api.html). News and map tiles
may be unavailable if their providers or your network block access.

## Tests

```powershell
python -m unittest -v
```
