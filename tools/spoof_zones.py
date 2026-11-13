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
import urllib.error
import urllib.request

# ---------------------------------------------------------------- параметры правила (по умолчанию)
V_ANY = 350.0      # км/ч: быстрее не ездит никто — прыжок в любом месте
V_ZONE = 150.0     # км/ч: прыжок в известную зону подмены
V_STILL = 100.0    # км/ч: прыжок, а трекер говорит, что стоит (скорость ниже STILL_KMH) — подмена
STILL_KMH = 5.0    # км/ч: скорость от трекера, ниже которой он «стоит»
V_STILL_ZONE = 30.0  # км/ч: то же для прыжка в зону подмены (короткие прыжки при редких точках на стоянке)
ANCHOR_MOVE_KM = 0.05  # км: точка без GPS, но сдвинулась дальше — настоящая (сотовая сеть, OBD), сдвигает время
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
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            sys.exit(f"Traccar: {method} {path} → HTTP {e.code}: {detail}")

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
        if d > min_jump and h > 0 and d / h > v_jump:
            out.append(b)
    return out


def hotspots(history, share=0.05, min_points=50):
    """Места стоянки устройств: ячейки 0,01°, где лежит больше share всех неподвижных точек устройства
    и не меньше min_points точек (иначе устройство с тремя точками за всё время объявляло бы
    стоянкой любое место — так зона подмены в Долгопрудном считалась стоянкой телефона ap)."""
    out = []
    for name, pts in history.items():
        still = [p for p in pts if p["speed"] < 3]
        cells = {}
        for p in still:
            cells.setdefault((round(p["lat"], 2), round(p["lon"], 2)), []).append(p)
        for key, cp in cells.items():
            if still and len(cp) / len(still) > share and len(cp) >= min_points:
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


def moving(p):
    """Может ли машина ехать: включено зажигание и это не автозапуск (атрибут autostart, например у Pandora).
    Датчику движения и скорости OBD не верим: у этих трекеров они ненадёжны. Без зажигания
    (телефоны, походные трекеры) — может."""
    if p.get("ign") is None:
        return True
    return bool(p.get("ign")) and not bool(p.get("auto"))


def replay(points, zones, v_any, v_zone, min_jump, v_still=V_STILL, parking=()):
    """Прогон истории устройства через правило — так же, как его выполняет Traccar.
    last — последняя точка, которую Traccar считает текущей (её обновляют только точки не старше её);
    anchor — время последней настоящей точки: замороженные и повторы координат без GPS его не сдвигают;
    idle — сколько из этого времени машина точно стояла (зажигание выключено, движения нет): за это время
    она никуда уехать не могла, и долгая подмена на стоянке не «отлипает» со временем."""
    last, frozen = None, []
    for p in points:
        spoofed = False
        idle_add = 0
        if last is not None:
            d = km(last["lat"], last["lon"], p["lat"], p["lon"])
            zone = in_zone(zones, p["lat"], p["lon"])
            chain = last["anchor"] != last["t"]          # прошлая точка заморожена или без GPS на старом месте
            if not moving(p) and not moving(last) and p["t"] > last["t"]:
                idle_add = p["t"] - last["t"]
            if parking and in_zone(parking, p["lat"], p["lon"]):
                # возврат на место стоянки — настоящая точка: подмена туда не уводит. Иначе после «протёкшей»
                # подменной точки машина залипала бы на ней, пока стоит дома с выключенным зажиганием
                spoofed = False
            elif p["t"] < last["t"]:
                # точка из буфера трекера старше текущей: сравнивать скорость не с чем, режем только зоны
                spoofed = "зона (буфер)" if zone and d > min_jump else False
            elif d > min_jump:
                h = (p["t"] - last["anchor"] - ((last["idle"] + idle_add) if chain else 0)) / 3600
                # во время подмены скорость от трекера тоже подменная: если датчик движения говорит «стоим»,
                # выход из заморозки проверяется по порогу «стоя»
                still = max(p.get("speed", 0), p.get("obd") or 0) < STILL_KMH or (last["spoofed"] and p.get("motion") is False)
                if d > v_any * h:
                    spoofed = "скорость"
                elif still and d > v_still * h:
                    spoofed = "прыжок стоя"
                elif zone:
                    spoofed = "удержание в зоне" if last["spoofed"] else (
                        "вход в зону" if d > v_zone * h or (still and d > V_STILL_ZONE * h) else False)
        cur = dict(p, spoofed=bool(spoofed))
        if spoofed:
            cur["lat"], cur["lon"] = last["lat"], last["lon"]
            cur["anchor"] = last["anchor"]
            frozen.append(dict(p, reason=spoofed))
        elif last is None or p.get("valid", True) or km(last["lat"], last["lon"], p["lat"], p["lon"]) > ANCHOR_MOVE_KM:
            cur["anchor"] = p["t"]
        else:
            cur["anchor"] = last["anchor"]          # без GPS на том же месте (тоннель, парковка): время не сдвигаем
        if cur["anchor"] == p["t"]:
            cur["idle"] = 0
        else:
            cur["idle"] = (last["idle"] if last["anchor"] != last["t"] else 0) + idle_add
        if last is None or p["t"] >= last["t"]:
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
MAX_EXPR = 4000      # tc_attributes.expression — VARCHAR(4000)


def jexl(zones, v_any, v_zone, min_jump, v_still=V_STILL, parking=()):
    """Вычисляемые атрибуты. Выражение в Traccar — не длиннее 4000 символов, поэтому общие части
    (расстояние, время, зоны, «может ехать») считаются отдельными атрибутами раньше и дальше
    используются по имени: контекст каждого атрибута видит уже посчитанные."""
    circle = lambda z: (f"math:hypot(110.57 * (latitude - {z['lat']}), "
                        f"{111.32 * math.cos(math.radians(z['lat'])):.4f} * (longitude - {z['lon']})) < {z['radius_km']}")
    dist = ("(lastLatitude ?? null) == null ? 0 : math:hypot(110.57 * (latitude - lastLatitude), "
            "111.32 * math:cos(math:toRadians(lastLatitude)) * (longitude - lastLongitude))")
    # может ли машина ехать: зажигание включено и это не автозапуск (autostart). Датчику движения и OBD не верим.
    # Нет зажигания (телефоны, походные трекеры) — может
    moving = "(ignition ?? null) == null ? true : ((ignition ?? false) ? ((autostart ?? false) ? false : true) : false)"
    # время последней настоящей точки: у замороженных и у повторов координат без GPS оно не сдвигается
    anchor = "(lastFixAnchor ?? lastFixTime.getTime())"
    chain = f"{anchor} != lastFixTime.getTime()"      # прошлая заморожена или без GPS на старом месте
    # интервал, когда машина точно стояла (обе точки без признаков движения): за него она не уедет
    idle_add = "(spoofMoving || (lastSpoofMoving ?? true) ? 0 : fixTime.getTime() - lastFixTime.getTime())"
    hours = (f"(lastFixTime ?? null) == null ? 0 : (fixTime.getTime() - {anchor} - "
             f"(({chain}) ? (lastIdleMs ?? 0) + {idle_add} : 0)) / 3600000.0")
    zone = " || ".join(circle(z) for z in zones) or "false"
    home = " || ".join(circle(z) for z in parking) or "false"
    # «стоит»: скорость от трекера и OBD ниже порога; во время подмены скорость тоже подменная, поэтому,
    # если датчик движения говорит «стоим», выход из заморозки проверяется по скорости «стоя».
    # Только по lastSpoofed нельзя: после одной замороженной точки замерзала вся поездка по трассе
    still = f"(math:max(speed, (obdSpeed ?? 0) / 1.852) < {STILL_KMH / 1.852:.2f} || ((lastSpoofed ?? false) && !(motion ?? true)))"
    faster = lambda v: f"spoofDist > {v} * spoofHours"      # без деления: интервал может быть нулевым
    # возврат на место стоянки — всегда настоящая точка (подмена туда не уводит); точка старше текущей
    # (буфер трекера) — только «в зоне и далеко»; точки без GPS (valid = false) проверяются так же
    spoofed = (f"(lastLatitude ?? null) == null || spoofHome ? false : "
               f"(fixTime.getTime() < lastFixTime.getTime() ? (spoofZone && spoofDist > {min_jump}) : "
               f"(spoofDist > {min_jump} && ({faster(v_any)} || ({still} && {faster(v_still)}) || "
               f"(spoofZone && ((lastSpoofed ?? false) || {faster(v_zone)} || ({still} && {faster(V_STILL_ZONE)}))))))")
    fix_anchor = (f"(spoofed ?? false) ? {anchor} : ((lastLatitude ?? null) == null || valid || spoofDist > {ANCHOR_MOVE_KM}"
                  f" ? fixTime.getTime() : (lastFixAnchor ?? fixTime.getTime()))")
    idle_ms = (f"(lastLatitude ?? null) == null || (fixAnchor ?? 0) == fixTime.getTime() ? 0 : "
               f"((({chain}) ? (lastIdleMs ?? 0) : 0) + {idle_add})")
    attrs = [
        # описание, атрибут, тип, приоритет (отрицательный — до фильтров; больше — раньше), выражение
        (f"{TAG}: может ехать", "spoofMoving", "boolean", -1, moving),
        (f"{TAG}: расстояние от прошлой", "spoofDist", "number", -2, dist),
        (f"{TAG}: время от настоящей точки", "spoofHours", "number", -3, hours),
        (f"{TAG}: в зоне подмены", "spoofZone", "boolean", -4, zone),
        (f"{TAG}: на месте стоянки", "spoofHome", "boolean", -5, home),
        (f"{TAG}: подмена GPS", "spoofed", "boolean", -6, spoofed),
        (f"{TAG}: широта", "latitude", "number", -7, "(spoofed ?? false) ? lastLatitude : latitude"),
        (f"{TAG}: долгота", "longitude", "number", -8, "(spoofed ?? false) ? lastLongitude : longitude"),
        (f"{TAG}: скорость", "speed", "number", -9, "(spoofed ?? false) ? 0 : speed"),
        (f"{TAG}: время настоящей точки", "fixAnchor", "number", -10, fix_anchor),
        (f"{TAG}: время стоянки", "idleMs", "number", -11, idle_ms),
    ]
    for desc, attr, typ, prio, expr in attrs:
        if len(expr) > MAX_EXPR:
            sys.exit(f"выражение «{desc}» длиннее {MAX_EXPR} символов ({len(expr)}): Traccar его не сохранит. "
                     f"Уменьшите число зон (--exclude) или объедините близкие.")
    return attrs


def diagnose(api, name, hours, zones, v_any, v_zone):
    """Разбор одного устройства: каждая точка за последние часы — интервал, расстояние, скорость прыжка,
    скорость и зажигание от трекера, зона, атрибут spoofed в базе и почему правило сработало или нет."""
    dev = next((d for d in api.call("GET", "/devices") if d["name"] == name), None)
    if dev is None:
        sys.exit(f"устройство {name!r} не найдено")
    now = dt.datetime.now(dt.timezone.utc)
    pts = sorted(api.positions(dev["id"], now - dt.timedelta(hours=hours), now), key=lambda p: p["fixTime"])
    attrs_seen = {}
    for p in pts:
        for k in (p.get("attributes") or {}):
            attrs_seen[k] = attrs_seen.get(k, 0) + 1
    print(f"{name}: {len(pts)} точек за {hours} ч. Атрибуты: " + ", ".join(f"{k} {v}" for k, v in sorted(attrs_seen.items())))
    print("время (UTC)          интервал   км    км/ч   скор зажиг движ  valid  зона  spoofed  координаты")
    prev = None
    for p in pts:
        at = p.get("attributes") or {}
        t = parse_time(p["fixTime"])
        line = dict(gap="", d="", v="")
        if prev:
            d = km(prev["latitude"], prev["longitude"], p["latitude"], p["longitude"])
            h = (t - parse_time(prev["fixTime"])) / 3600
            line = dict(gap=f"{h * 60:6.1f}м", d=f"{d:6.2f}", v=f"{d / h:6.0f}" if h > 0 else "   —  ")
        z = next((i + 1 for i, zz in enumerate(zones) if km(zz["lat"], zz["lon"], p["latitude"], p["longitude"]) < zz["radius_km"]), "")
        print(f"{p['fixTime'][:19]}  {line['gap']:>8} {line['d']:>6} {line['v']:>6}  {(p.get('speed') or 0) * 1.852:5.0f} "
              f"{str(at.get('ignition', '—')):>5} {str(at.get('motion', '—')):>5} {str(p.get('valid')):>6} {str(z):>4}  "
              f"{str(at.get('spoofed', '—')):>7}  {p['latitude']:.5f},{p['longitude']:.5f}")
        prev = p


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
    ap.add_argument("--include", action="append", default=[], metavar="LAT,LON",
                    help="применять зону, даже если она исключена (например, стоянка другого устройства)")
    ap.add_argument("--save-history", help="сохранить скачанную историю в файл JSON")
    ap.add_argument("--history", help="взять историю из файла (без запросов к истории в Traccar)")
    ap.add_argument("--map", help="записать HTML-карту: зоны и замороженные/оставленные точки")
    ap.add_argument("--diagnose", metavar="УСТРОЙСТВО", help="построчный разбор точек устройства (с --hours и --zones)")
    ap.add_argument("--hours", type=int, default=24, help="для --diagnose: сколько часов смотреть")
    ap.add_argument("--v-any", type=float, default=V_ANY)
    ap.add_argument("--v-zone", type=float, default=V_ZONE)
    ap.add_argument("--v-still", type=float, default=V_STILL)
    ap.add_argument("--apply", action="store_true", help="создать или обновить вычисляемые атрибуты в Traccar")
    ap.add_argument("--group-id", type=int, help="привязать атрибуты к группе устройств")
    a = ap.parse_args()

    api = Api(a.url, a.token)
    if a.diagnose:
        zones = [z for z in json.load(open(a.zones, encoding="utf-8")) if not z.get("excluded")] if a.zones else []
        return diagnose(api, a.diagnose, a.hours, zones, a.v_any, a.v_zone)
    now = dt.datetime.now(dt.timezone.utc)
    history = {}
    devices = [] if a.history else api.call("GET", "/devices")
    if a.history:
        history = json.load(open(a.history, encoding="utf-8"))
    for d in devices:
        pts = api.positions(d["id"], now - dt.timedelta(days=a.days), now)
        pts = [{"dev": d["name"], "t": parse_time(p["fixTime"]), "lat": p["latitude"], "lon": p["longitude"],
                "speed": (p.get("speed") or 0) * 1.852, "valid": p.get("valid", True),
                **{k: (p.get("attributes") or {}).get(a) for k, a in (("ign", "ignition"), ("motion", "motion"),
                                                                      ("obd", "obdSpeed"), ("rpm", "rpm"),
                                                                      ("auto", "autostart"))}}
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
            return zs

        zones = make_zones(jp)
    for inc in a.include:
        la, lo = (float(x) for x in inc.split(","))
        for z in zones:
            if km(z["lat"], z["lon"], la, lo) < z["radius_km"]:
                z.pop("excluded", None)
    for ex in a.exclude:
        la, lo = (float(x) for x in ex.split(","))
        for z in zones:
            if km(z["lat"], z["lon"], la, lo) < z["radius_km"]:
                z["excluded"] = "--exclude"

    print("\nЗоны подмены (центр, радиус):")
    for z in zones:
        print(f"  {'—' if z.get('excluded') else '+'} {z['lat']:.5f}, {z['lon']:.5f}  r={z['radius_km']} км"
              + (f"  прыжков {z['episodes']}, устройств {z['devices']}, в движении {int(z['moving'] * 100)} %"
                 if "episodes" in z else "")
              + (f"  {z['note']}" if z.get("note") else "")
              + (f"  ИСКЛЮЧЕНА: {z['excluded']}" if z.get("excluded") else ""))
    all_zones, zones = zones, [z for z in zones if not z.get("excluded")]
    parking = [z for z in all_zones if str(z.get("excluded", "")).startswith("место стоянки")]
    if a.apply and not zones:
        sys.exit("нет зон для применения")
    if a.save:
        json.dump(all_zones, open(a.save, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    print("\nПроверка правила на истории (сколько точек заморозилось бы, по причинам):")
    frozen_all = {}
    for name, pts in history.items():
        fr = replay(pts, zones, a.v_any, a.v_zone, MIN_JUMP, a.v_still, parking)
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

    attrs = jexl(zones, a.v_any, a.v_zone, MIN_JUMP, a.v_still, parking)
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
