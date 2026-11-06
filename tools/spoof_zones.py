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
def jumps(points, v_jump, min_jump):
    """Точки, в которые устройство «прыгнуло»: дальше min_jump от прошлой точки быстрее v_jump.
    Поездка такой точки не даёт: соседние точки при езде близко. Прыжки обратно на настоящее место
    (дом, работа) тоже попадают сюда — такие места потом отсекает проверка мест стоянки."""
    out = []
    for a, b in zip(points, points[1:]):
        d = km(a["lat"], a["lon"], b["lat"], b["lon"])
        h = (b["t"] - a["t"]) / 3600
        if d > min_jump and (h <= 0 or d / h > v_jump):
            out.append(b)
    return out


def hotspots(history, share=0.05):
    """Места стоянки устройств: ячейки 0,01°, где лежит больше share всех неподвижных точек устройства."""
    out = []
    for name, pts in history.items():
        still = [p for p in pts if p["speed"] < 3]
        cells = {}
        for p in still:
            cells.setdefault((round(p["lat"], 2), round(p["lon"], 2)), []).append(p)
        for key, cp in cells.items():
            if still and len(cp) / len(still) > share:
                out.append({"dev": name, "lat": sum(p["lat"] for p in cp) / len(cp),
                            "lon": sum(p["lon"] for p in cp) / len(cp), "share": len(cp) / len(still)})
    return out


def cluster(points, merge_km, min_points, min_episodes):
    """Жадная кластеризация точек прыжков: сетка 0,01°, слияние соседних ячеек ближе merge_km.
    Каждая точка — отдельный прыжок; min_episodes — сколько прыжков нужно для зоны."""
    cells = {}
    for k, p in enumerate(points):
        key = (round(p["lat"], 2), round(p["lon"], 2))
        c = cells.setdefault(key, {"pts": [], "eps": set(), "devs": set()})
        c["pts"].append(p)
        c["eps"].add(k)
        c["devs"].add(p["dev"])
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
        moving = sum(1 for p in z["pts"] if p["speed"] >= 3) / len(z["pts"])
        out.append({"lat": round(z["lat"], 5), "lon": round(z["lon"], 5), "radius_km": round(min(max(r, 1.0), 8.0), 2),
                    "points": len(z["pts"]), "episodes": len(z["eps"]), "devices": len(z["devs"]),
                    "moving": round(moving, 2)})
    return out


# ---------------------------------------------------------------- правило: то же, что выражения в Traccar
def in_zone(zones, lat, lon):
    return any(km(z["lat"], z["lon"], lat, lon) < z["radius_km"] for z in zones)


def replay(points, zones, v_any, v_zone, min_jump):
    """Прогон истории устройства через правило. last — последняя сохранённая (возможно, замороженная) точка."""
    last, frozen = None, []
    for p in points:
        spoofed = False
        if last is not None and p.get("valid", True):
            d = km(last["lat"], last["lon"], p["lat"], p["lon"])
            h = (p["t"] - last["t"]) / 3600
            v = d / h if h > 0 else (1e9 if d > min_jump else 0)
            # в зоне: прыжок в неё или продолжение подмены — но только пока точка далеко от замороженного места
            if d > min_jump:
                if v > v_any:
                    spoofed = "скорость" if h > 0 else "время назад"
                elif in_zone(zones, p["lat"], p["lon"]):
                    spoofed = "удержание в зоне" if last["spoofed"] else ("вход в зону" if v > v_zone else False)
        cur = dict(p, spoofed=bool(spoofed))
        if spoofed:
            cur["lat"], cur["lon"] = last["lat"], last["lon"]
            frozen.append(dict(p, reason=spoofed))
        last = cur
    return frozen


# ---------------------------------------------------------------- карта для проверки глазами
def write_map(path, history, frozen_all, zones, days=3):
    """Leaflet: зоны (красные — применяются, серые — исключены), последние дни каждого устройства:
    синие — оставленные точки, красные — замороженные."""
    layers = {}
    for name, pts in history.items():
        if not pts:
            continue
        t0 = pts[-1]["t"] - days * 86400
        fr = {(p["t"], p["lat"], p["lon"]) for p in frozen_all.get(name, [])}
        keep = [[round(p["lat"], 5), round(p["lon"], 5)] for p in pts if p["t"] >= t0 and (p["t"], p["lat"], p["lon"]) not in fr]
        bad = [[round(p["lat"], 5), round(p["lon"], 5)] for p in pts if p["t"] >= t0 and (p["t"], p["lat"], p["lon"]) in fr]
        layers[name] = {"keep": keep[-15000:], "bad": bad[-15000:]}
    html = """<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>Зоны подмены GPS</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>html,body,#m{height:100%;margin:0}</style></head><body><div id="m"></div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script><script>
const Z=__ZONES__, L_=__LAYERS__;
const m=L.map('m',{preferCanvas:true}).setView([55.9,37.4],9);
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:18,attribution:'© OpenStreetMap'}).addTo(m);
const over={};
const zl=L.layerGroup(Z.map(z=>L.circle([z.lat,z.lon],{radius:z.radius_km*1000,color:z.excluded?'#888':'#d00',weight:2,fillOpacity:.08})
  .bindPopup(`${z.lat}, ${z.lon}<br>r=${z.radius_km} км<br>прыжков ${z.episodes??'—'}<br>${z.excluded?'ИСКЛЮЧЕНА: '+z.excluded:'применяется'}`))).addTo(m);
over['Зоны']=zl;
for(const [n,l] of Object.entries(L_)){
  const g=L.layerGroup([...l.keep.map(p=>L.circleMarker(p,{radius:2,color:'#06c',weight:1})),
                        ...l.bad.map(p=>L.circleMarker(p,{radius:2,color:'#d00',weight:1}))]);
  over[`${n}: оставлено ${l.keep.length}, заморожено ${l.bad.length}`]=g;
}
L.control.layers(null,over,{collapsed:false}).addTo(m);
</script></body></html>"""
    html = html.replace("__ZONES__", json.dumps(zones, ensure_ascii=False)).replace("__LAYERS__", json.dumps(layers, ensure_ascii=False))
    open(path, "w", encoding="utf-8").write(html)


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
    ap.add_argument("--min-episodes", type=int, default=10, help="меньше прыжков в зону — случайность, а не зона")
    ap.add_argument("--exclude", action="append", default=[], metavar="LAT,LON",
                    help="убрать зону, в которую попадает точка (можно несколько раз)")
    ap.add_argument("--save-history", help="сохранить скачанную историю в файл JSON")
    ap.add_argument("--history", help="взять историю из файла (без запросов к истории в Traccar)")
    ap.add_argument("--map", help="записать HTML-карту: зоны и замороженные/оставленные точки")
    ap.add_argument("--v-any", type=float, default=V_ANY)
    ap.add_argument("--v-zone", type=float, default=V_ZONE)
    ap.add_argument("--apply", action="store_true", help="создать или обновить вычисляемые атрибуты в Traccar")
    ap.add_argument("--group-id", type=int, help="привязать атрибуты к группе устройств")
    a = ap.parse_args()

    api = Api(a.url, a.token)
    now = dt.datetime.now(dt.timezone.utc)
    history = {}
    devices = [] if a.history else api.call("GET", "/devices")
    if a.history:
        history = json.load(open(a.history, encoding="utf-8"))
    for d in devices:
        pts = api.positions(d["id"], now - dt.timedelta(days=a.days), now)
        pts = [{"dev": d["name"], "t": parse_time(p["fixTime"]), "lat": p["latitude"], "lon": p["longitude"],
                "speed": (p.get("speed") or 0) * 1.852, "valid": p.get("valid", True)}
               for p in pts if p.get("latitude") or p.get("longitude")]
        history[d["name"]] = sorted(pts, key=lambda p: p["t"])
        print(f"{d['name']}: {len(pts)} точек", file=sys.stderr)
    if a.save_history:
        json.dump(history, open(a.save_history, "w", encoding="utf-8"))

    if a.zones:
        zones = json.load(open(a.zones, encoding="utf-8"))   # зоны с "excluded" в файле не применяются
    else:
        jp = [p for pts in history.values() for p in jumps(pts, a.v_any, MIN_JUMP)]
        print(f"прыжков быстрее {a.v_any:.0f} км/ч: {len(jp)}", file=sys.stderr)
        homes = hotspots(history)

        def make_zones(eps):   # eps — точки прыжков
            zs = cluster(eps, merge_km=4, min_points=a.min_episodes, min_episodes=a.min_episodes)
            # зона, в которой устройство обычно стоит, — это его стоянка, а не подмена
            for z in zs:
                near = sorted({h["dev"] for h in homes if km(z["lat"], z["lon"], h["lat"], h["lon"]) < z["radius_km"] + 0.5})
                if near:
                    z["excluded"] = "место стоянки: " + ", ".join(near)
                elif z.get("moving", 1) < 0.5:
                    z["excluded"] = f"точки неподвижны ({int((1 - z['moving']) * 100)} %)"
            return zs

        zones = make_zones(jp)
    for ex in a.exclude:
        la, lo = (float(x) for x in ex.split(","))
        for z in zones:
            if km(z["lat"], z["lon"], la, lo) < z["radius_km"]:
                z["excluded"] = "--exclude"

    print("\nЗоны подмены (центр, радиус):")
    for z in zones:
        print(f"  {'—' if z.get('excluded') else '+'} {z['lat']:.5f}, {z['lon']:.5f}  r={z['radius_km']} км"
              + (f"  прыжков {z['episodes']}, устройств {z['devices']}, в движении {int(z['moving'] * 100)} %"
                 if "points" in z else "")
              + (f"  ИСКЛЮЧЕНА: {z['excluded']}" if z.get("excluded") else ""))
    all_zones, zones = zones, [z for z in zones if not z.get("excluded")]
    if a.apply and not zones:
        sys.exit("нет зон для применения")
    if a.save:
        json.dump(all_zones, open(a.save, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    print("\nПроверка правила на истории (сколько точек заморозилось бы, по причинам):")
    frozen_all = {}
    for name, pts in history.items():
        fr = replay(pts, zones, a.v_any, a.v_zone, MIN_JUMP)
        frozen_all[name] = fr
        if pts:
            reasons = {}
            for p in fr:
                reasons[p["reason"]] = reasons.get(p["reason"], 0) + 1
            print(f"  {name}: {len(fr)} из {len(pts)} ({100 * len(fr) // len(pts)} %)"
                  + ("  — " + ", ".join(f"{k} {v}" for k, v in sorted(reasons.items(), key=lambda kv: -kv[1])) if reasons else ""))
    if a.map:
        write_map(a.map, history, frozen_all, all_zones)
        print(f"\nКарта: {a.map}", file=sys.stderr)

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
