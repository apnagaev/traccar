#!/usr/bin/env python3
"""Зоны подмены GPS (спуфинга) по истории Traccar и вычисляемые атрибуты, которые «замораживают» такие точки.

Что происходит при подмене: трекер получает поддельный сигнал и отправляет точки в одной из нескольких
постоянных зон (обычно у аэропортов и охраняемых объектов), часто по кругу, хотя машина стоит.
Признак: прыжок от настоящего места в зону с невозможной скоростью, дальше — движение внутри зоны.

Скрипт:
  1. берёт историю всех устройств через API Traccar;
  2. находит такие прыжки и точки после них, собирает их в зоны (центр + радиус);
  3. печатает зоны и выражения вычисляемых атрибутов для Traccar;
  4. проверяет правило на той же истории: сколько точек заморозилось бы, у каких устройств;
  5. с --apply создаёт/обновляет атрибуты в Traccar (и с --group-id привязывает к группе).

Только стандартная библиотека Python 3.8+.

  python3 spoof_zones.py --url https://track.example.com --token TOKEN --days 60
  python3 spoof_zones.py --url ... --token ... --days 60 --save zones.json
  python3 spoof_zones.py --url ... --token ... --zones zones.json --apply --group-id 10

Токен: в Traccar «Настройки → Учётная запись → Токен» (пользователь-администратор, чтобы видеть все устройства).
"""
import argparse
import datetime as dt
import json
import math
import sys
import urllib.parse
import urllib.request

# ---------------------------------------------------------------- параметры правила (по умолчанию)
V_ANY = 350.0      # км/ч: быстрее не ездит никто — прыжок в любом месте
V_ZONE = 150.0     # км/ч: прыжок в известную зону подмены
MIN_JUMP = 1.0     # км: короче — шум GPS при малом интервале, а не прыжок
TAG = "antispoof"  # префикс описаний атрибутов в Traccar


def km(lat1, lon1, lat2, lon2):
    """Расстояние, км (равнопромежуточная проекция — точно на десятках км, так же считает выражение в Traccar)."""
    return math.hypot(110.57 * (lat2 - lat1), 111.32 * math.cos(math.radians(lat1)) * (lon2 - lon1))


def parse_time(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


# ---------------------------------------------------------------- API Traccar
class Api:
    def __init__(self, url, token):
        self.base = url.rstrip("/") + "/api"
        self.token = token

    def call(self, method, path, params=None, body=None):
        url = self.base + path + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", "Bearer " + self.token)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=300) as r:
            raw = r.read()
            return json.loads(raw) if raw else None

    def positions(self, device_id, t_from, t_to):
        """История по суткам, чтобы не упираться в лимит ответа сервера."""
        out, t = [], t_from
        while t < t_to:
            t2 = min(t + dt.timedelta(days=1), t_to)
            out += self.call("GET", "/positions", {"deviceId": device_id,
                                                   "from": t.isoformat().replace("+00:00", "Z"),
                                                   "to": t2.isoformat().replace("+00:00", "Z")}) or []
            t = t2
        return out


# ---------------------------------------------------------------- поиск эпизодов подмены
def episodes(points, v_jump, min_jump, max_hours):
    """Эпизод: прыжок со скоростью > v_jump от «якоря» (последней нормальной точки), затем точки,
    пока трекер не вернётся к якорю (ближе min_jump) или не пройдёт max_hours."""
    eps, i, n = [], 1, len(points)
    while i < n:
        a, b = points[i - 1], points[i]
        d = km(a["lat"], a["lon"], b["lat"], b["lon"])
        h = (b["t"] - a["t"]) / 3600
        if d > min_jump and (h <= 0 or d / h > v_jump):
            anchor, ep, j, back = a, [], i, False
            while j < n and points[j]["t"] - b["t"] < max_hours * 3600:
                p = points[j]
                if km(anchor["lat"], anchor["lon"], p["lat"], p["lon"]) < min_jump:
                    back = True
                    break
                ep.append(p)
                j += 1
            if back:                        # вернулся к якорю — значит, это был не переезд
                eps.append({"anchor": anchor, "points": ep})
                i = j + 1                   # обратный прыжок к якорю — не новый эпизод
            else:
                i += 1
        else:
            i += 1
    return eps


def cluster(eps, merge_km, min_points, min_episodes):
    """Жадная кластеризация точек эпизодов: сетка 0,01°, слияние соседних ячеек ближе merge_km."""
    cells = {}
    for k, e in enumerate(eps):
        for p in e["points"]:
            key = (round(p["lat"], 2), round(p["lon"], 2))
            c = cells.setdefault(key, {"pts": [], "eps": set(), "devs": set()})
            c["pts"].append(p)
            c["eps"].add(k)
            c["devs"].add(e["anchor"]["dev"])
    zones = []
    for key, c in sorted(cells.items(), key=lambda kv: -len(kv[1]["pts"])):
        for z in zones:
            if km(z["lat"], z["lon"], key[0], key[1]) < merge_km:
                z["pts"] += c["pts"]; z["eps"] |= c["eps"]; z["devs"] |= c["devs"]
                z["lat"] = sum(p["lat"] for p in z["pts"]) / len(z["pts"])
                z["lon"] = sum(p["lon"] for p in z["pts"]) / len(z["pts"])
                break
        else:
            zones.append({"lat": key[0], "lon": key[1], "pts": list(c["pts"]), "eps": set(c["eps"]), "devs": set(c["devs"])})
    out = []
    for z in zones:
        if len(z["pts"]) < min_points or len(z["eps"]) < min_episodes:
            continue
        ds = sorted(km(z["lat"], z["lon"], p["lat"], p["lon"]) for p in z["pts"])
        r = ds[int(0.95 * (len(ds) - 1))] + 0.5
        out.append({"lat": round(z["lat"], 5), "lon": round(z["lon"], 5), "radius_km": round(min(max(r, 1.0), 8.0), 2),
                    "points": len(z["pts"]), "episodes": len(z["eps"]), "devices": len(z["devs"])})
    return out


# ---------------------------------------------------------------- правило: то же, что выражения в Traccar
def in_zone(zones, lat, lon):
    return any(km(z["lat"], z["lon"], lat, lon) < z["radius_km"] for z in zones)


def replay(points, zones, v_any, v_zone, min_jump):
    """Прогон истории устройства через правило. last — последняя сохранённая (возможно, замороженная) точка."""
    last, frozen = None, []
    for p in points:
        spoofed = False
        if last is not None:
            d = km(last["lat"], last["lon"], p["lat"], p["lon"])
            h = (p["t"] - last["t"]) / 3600
            v = d / h if h > 0 else (1e9 if d > min_jump else 0)
            # в зоне: прыжок в неё или продолжение подмены — но только пока точка далеко от замороженного места
            spoofed = d > min_jump and (v > v_any or (
                in_zone(zones, p["lat"], p["lon"]) and (last["spoofed"] or v > v_zone)))
        cur = dict(p, spoofed=spoofed)
        if spoofed:
            cur["lat"], cur["lon"] = last["lat"], last["lon"]
            frozen.append(p)
        last = cur
    return frozen


# ---------------------------------------------------------------- выражения для Traccar (JEXL)
def jexl(zones, v_any, v_zone, min_jump):
    dist = ("math:hypot(110.57 * (latitude - lastLatitude), "
            "111.32 * math:cos(math:toRadians(lastLatitude)) * (longitude - lastLongitude))")
    hours = "((fixTime.getTime() - lastFixTime.getTime()) / 3600000.0)"
    zone = " || ".join(
        f"math:hypot(110.57 * (latitude - {z['lat']}), {111.32 * math.cos(math.radians(z['lat'])):.4f} * (longitude - {z['lon']})) < {z['radius_km']}"
        for z in zones) or "false"
    speed = f"({hours} <= 0 || {dist} / {hours} > {{v}})"
    # точки без GPS (valid = false) не трогаем: их координаты потом считает геолокация по вышкам
    spoofed = (f"(lastLatitude ?? null) == null || !valid ? false : ({dist} > {min_jump} && ("
               f"{speed.format(v=v_any)} || (({zone}) && ((lastSpoofed ?? false) || {speed.format(v=v_zone)}))))")
    return [
        # описание, атрибут, тип, приоритет (отрицательный — до фильтров), выражение
        (f"{TAG}: подмена GPS", "spoofed", "boolean", -1, spoofed),
        (f"{TAG}: широта", "latitude", "number", -2, "(spoofed ?? false) ? lastLatitude : latitude"),
        (f"{TAG}: долгота", "longitude", "number", -3, "(spoofed ?? false) ? lastLongitude : longitude"),
        (f"{TAG}: скорость", "speed", "number", -4, "(spoofed ?? false) ? 0 : speed"),
    ]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True)
    ap.add_argument("--token", required=True)
    ap.add_argument("--days", type=int, default=60, help="сколько дней истории смотреть")
    ap.add_argument("--zones", help="взять зоны из файла, а не считать по истории")
    ap.add_argument("--save", help="сохранить найденные зоны в файл JSON")
    ap.add_argument("--min-points", type=int, default=30)
    ap.add_argument("--min-episodes", type=int, default=3)
    ap.add_argument("--v-any", type=float, default=V_ANY)
    ap.add_argument("--v-zone", type=float, default=V_ZONE)
    ap.add_argument("--apply", action="store_true", help="создать или обновить вычисляемые атрибуты в Traccar")
    ap.add_argument("--group-id", type=int, help="привязать атрибуты к группе устройств")
    a = ap.parse_args()

    api = Api(a.url, a.token)
    now = dt.datetime.now(dt.timezone.utc)
    devices = api.call("GET", "/devices")
    history = {}
    for d in devices:
        pts = api.positions(d["id"], now - dt.timedelta(days=a.days), now)
        pts = [{"dev": d["name"], "t": parse_time(p["fixTime"]), "lat": p["latitude"], "lon": p["longitude"]}
               for p in pts if p.get("latitude") or p.get("longitude")]
        history[d["name"]] = sorted(pts, key=lambda p: p["t"])
        print(f"{d['name']}: {len(pts)} точек", file=sys.stderr)

    if a.zones:
        zones = json.load(open(a.zones, encoding="utf-8"))
    else:
        eps = [e for pts in history.values() for e in episodes(pts, a.v_any, MIN_JUMP, 12)]
        print(f"эпизодов подмены: {len(eps)}", file=sys.stderr)
        zones = cluster(eps, merge_km=4, min_points=a.min_points, min_episodes=a.min_episodes)

    print("\nЗоны подмены (центр, радиус):")
    for z in zones:
        print(f"  {z['lat']:.5f}, {z['lon']:.5f}  r={z['radius_km']} км"
              + (f"  точек {z['points']}, эпизодов {z['episodes']}, устройств {z['devices']}" if "points" in z else ""))
    if a.save:
        json.dump(zones, open(a.save, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    print("\nПроверка правила на истории (сколько точек заморозилось бы):")
    for name, pts in history.items():
        fr = replay(pts, zones, a.v_any, a.v_zone, MIN_JUMP)
        if pts:
            print(f"  {name}: {len(fr)} из {len(pts)}")

    attrs = jexl(zones, a.v_any, a.v_zone, MIN_JUMP)
    print("\nВычисляемые атрибуты (Настройки → Атрибуты; порядок важен — приоритет):")
    for desc, attr, typ, prio, expr in attrs:
        print(f"\n# {desc}  [атрибут {attr}, тип {typ}, приоритет {prio}]\n{expr}")

    if a.apply:
        existing = {x["description"]: x for x in api.call("GET", "/attributes/computed") or []}
        for desc, attr, typ, prio, expr in attrs:
            body = {"description": desc, "attribute": attr, "type": typ, "priority": prio, "expression": expr}
            if desc in existing:
                body["id"] = existing[desc]["id"]
                res = api.call("PUT", f"/attributes/computed/{body['id']}", body=body)
            else:
                res = api.call("POST", "/attributes/computed", body=body)
            print(f"сохранён: {desc} (id {res['id']})", file=sys.stderr)
            if a.group_id and desc not in existing:
                api.call("POST", "/permissions", body={"groupId": a.group_id, "attributeId": res["id"]})


if __name__ == "__main__":
    main()
