"""Open-Meteo'dan hava durumu: şehir arama ve tahmin (yalnızca urllib).

Dışarı giden bilgi: şehir aramasında yalnızca yazılan ad, tahminde yalnızca iki basamağa yuvarlanmış
enlem ve boylam. Başka hiçbir şey gönderilmez.
"""
from __future__ import annotations

import http.client
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

import i18n

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
USER_AGENT = "weather-client/1.0"
TIMEOUT = 10

# WMO kodu -> arayüzde gösterilen metin (Türkçe kaynak; gösterilirken condition() çevirir)
WMO_TEXT = {
    0: "açık", 1: "az bulutlu", 2: "parçalı bulutlu", 3: "kapalı", 45: "sisli", 48: "kırağılı sis",
    51: "hafif çisenti", 53: "çisenti", 55: "yoğun çisenti", 56: "donan çisenti", 57: "donan çisenti",
    61: "hafif yağmur", 63: "yağmur", 65: "kuvvetli yağmur", 66: "donan yağmur", 67: "donan yağmur",
    71: "hafif kar", 73: "kar", 75: "yoğun kar", 77: "kar taneleri",
    80: "hafif sağanak", 81: "sağanak", 82: "kuvvetli sağanak", 85: "kar sağanağı", 86: "yoğun kar sağanağı",
    95: "gök gürültülü fırtına", 96: "dolu ile fırtına", 99: "dolu ile fırtına",
}


def condition(code) -> str | None:
    """Kodun, seçili dildeki metni."""
    return i18n.t(WMO_TEXT[code]) if code in WMO_TEXT else None


class WeatherError(Exception):
    """İletisi kullanıcıya gösterilir."""


def http_json(url: str, params: dict) -> dict:
    """Testler bu işlevi değiştirir; ağa çıkan tek yer burasıdır."""
    req = urllib.request.Request(url + "?" + urllib.parse.urlencode(params),
                                 headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read())


def _get(url: str, params: dict, what: str) -> dict:
    try:
        data = http_json(url, params)
    except urllib.error.HTTPError as exc:
        raise WeatherError(i18n.t("{what}: sunucu hata verdi ({code}).", what=what, code=exc.code)) from None
    except (socket.timeout, TimeoutError):
        raise WeatherError(i18n.t("{what}: sunucu yanıt vermedi.", what=what)) from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            raise WeatherError(i18n.t("{what}: sunucu yanıt vermedi.", what=what)) from None
        raise WeatherError(i18n.t("{what}: internet yok.", what=what)) from None
    except OSError:
        raise WeatherError(i18n.t("{what}: internet yok.", what=what)) from None
    except (ValueError, http.client.HTTPException):
        raise WeatherError(i18n.t("{what}: beklenmeyen yanıt.", what=what)) from None
    if not isinstance(data, dict):
        raise WeatherError(i18n.t("{what}: beklenmeyen yanıt.", what=what))
    return data


def geocode(name: str) -> list[dict]:
    """Şehir adına uyan en çok 5 aday: ad, il/bölge, ülke, iki basamağa yuvarlanmış enlem ve boylam."""
    data = _get(GEOCODE_URL, {"name": name, "count": 5, "language": i18n.lang()}, i18n.t("Şehir aranamadı"))
    out = []
    for r in data.get("results") or []:
        try:
            out.append({"name": str(r["name"]), "admin": str(r.get("admin1") or ""), "country": str(r.get("country") or ""),
                        "lat": round(float(r["latitude"]), 2), "lon": round(float(r["longitude"]), 2)})
        except (KeyError, TypeError, ValueError):
            continue
    return out


def forecast(lat: float, lon: float, city: str, now: float | None = None) -> dict:
    """Anlık durum, 7 gün ve önümüzdeki saatler; bandmsg.weather_* işlevlerinin beklediği sözlük."""
    what = i18n.t("Hava durumu alınamadı")
    data = _get(FORECAST_URL, {
        "latitude": f"{round(float(lat), 2):.2f}", "longitude": f"{round(float(lon), 2):.2f}",
        "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m,wind_direction_10m,pressure_msl",
        "hourly": "temperature_2m,weather_code,wind_speed_10m,wind_direction_10m,uv_index",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,sunrise,sunset",
        "forecast_days": 7, "timezone": "auto", "timeformat": "unixtime", "wind_speed_unit": "kmh",
    }, what)
    now = int(now if now is not None else time.time())
    try:
        cur, hourly, daily = data["current"], data["hourly"], data["daily"]
        out = {"city": city, "ts": int(cur.get("time") or now), "code": int(cur["weather_code"]),
               "temp": float(cur["temperature_2m"]), "humidity": cur.get("relative_humidity_2m"),
               "wind": cur.get("wind_speed_10m"), "wind_dir": cur.get("wind_direction_10m"),
               "pressure": cur.get("pressure_msl"), "uv": None, "aqi": None, "days": [], "hours": []}
        for i in range(len(daily["time"])):
            if daily["temperature_2m_max"][i] is None or daily["temperature_2m_min"][i] is None:
                continue
            out["days"].append({"code": int(daily["weather_code"][i] if daily["weather_code"][i] is not None else 3),
                                "max": float(daily["temperature_2m_max"][i]), "min": float(daily["temperature_2m_min"][i]),
                                "sunrise": int(daily["sunrise"][i] or 0), "sunset": int(daily["sunset"][i] or 0)})
        uv = hourly.get("uv_index") or []
        for i, ts in enumerate(hourly["time"]):
            if ts <= now:
                if now - ts < 3600 and i < len(uv) and uv[i] is not None:
                    out["uv"] = float(uv[i])  # içinde bulunulan saatin değeri
                continue
            if hourly["temperature_2m"][i] is None or hourly["weather_code"][i] is None:
                continue
            out["hours"].append({"ts": int(ts), "temp": float(hourly["temperature_2m"][i]), "code": int(hourly["weather_code"][i]),
                                 "wind": hourly["wind_speed_10m"][i], "wind_dir": hourly["wind_direction_10m"][i]})
            if len(out["hours"]) >= 23:
                break
    except (KeyError, TypeError, ValueError, IndexError):
        raise WeatherError(i18n.t("{what}: beklenmeyen yanıt.", what=what)) from None
    return out
