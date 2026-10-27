"""
極端天氣警示 → Discord 推播
資料來源：NCDR 災害示警公開資料平台 ATOM feed（不需金鑰）
  https://alerts.ncdr.nat.gov.tw/RssAtomFeed.ashx
內容即中央氣象署發布的颱風警報、強風、降雨（大雨/豪雨）等 CAP 示警。

只用 Python 標準函式庫，GitHub Actions 不用裝任何套件。

環境變數：
  DISCORD_WEBHOOK_URL   必填，Discord Webhook
  SITE_COUNTIES         工地所在縣市，逗號分隔，預設「臺北市,新北市」
  RAIN_KEYWORDS         降雨特報比對用的區域關鍵字（降雨文字常寫「桃園以北」「北部」）
  CATEGORIES            要監看的示警類別，預設「颱風,強風,降雨」
  TYPHOON_REMIND_HOURS  颱風狀態沒變時，多久再提醒一次，預設 3
  QUIET_START/QUIET_END 安靜時段，預設 19 / 7（只放行嚴重示警）
  DISCORD_MENTION       要 @ 的對象，例如 @everyone 或 <@&角色ID>，可留空
  STATE_FILE            狀態檔，預設 weather_alert/state.json
  NCDR_FEED_FILE        測試用：改讀本機 XML
  DRY_RUN=1             只印出不送出
  FORCE=1               忽略安靜時段與去重，直接送目前狀態（測試用）
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

FEED_URL = "https://alerts.ncdr.nat.gov.tw/RssAtomFeed.ashx"
WATCH_URL = "https://watch.ncdr.nat.gov.tw/watch_typhoon"
TW = timezone(timedelta(hours=8))
NS = {"a": "http://www.w3.org/2005/Atom", "cap": "urn:oasis:names:tc:emergency:cap:1.1"}

COLOR = {"red": 0xE53935, "orange": 0xFB8C00, "yellow": 0xFDD835,
         "blue": 0x1E88E5, "green": 0x43A047, "grey": 0x9E9E9E}
LEVEL_RANK = {"none": 0, "yellow": 1, "orange": 2, "red": 3}
LEVEL_NAME = {"none": "無", "yellow": "黃色燈號", "orange": "橙色燈號", "red": "紅色燈號"}
RAIN_RANK = {"大雨": 1, "豪雨": 2, "大豪雨": 3, "超大豪雨": 4}

SITE_ACTIONS = {
    "颱風": [
        "塔吊／吊車停止作業，吊臂鬆開迴轉剎車讓它隨風轉",
        "鷹架帆布、防塵網收捲或開口，圍籬與告示牌加固",
        "高處鬆散材料、模板、鐵皮綁紮或收進室內",
        "抽水機、發電機試車待命，截水溝與集水井清淤",
        "開挖面與邊坡覆蓋帆布，留意擋土監測數據",
        "移工宿舍／工寮檢查，必要時撤離並清點人數",
    ],
    "強風": [
        "依規定停止吊掛與高處作業",
        "鷹架帆布、防塵網、圍籬檢查加固",
        "收好輕質材料與工具，避免被吹落",
    ],
    "降雨": [
        "抽水機待命，截水溝、沉砂池清淤",
        "開挖面與邊坡覆蓋，留意地下水位與擋土監測",
        "土車出場路面泥濘，洗車台與道路清潔加強",
        "雷雨時停止戶外高處與吊掛作業",
    ],
}


# ---------------------------------------------------------------- 工具
def env(name: str, default: str = "") -> str:
    """空字串也視為沒設定（GitHub Actions 沒設的 vars 會傳空字串）"""
    return (os.environ.get(name) or "").strip() or default


def norm(s: str) -> str:
    """台→臺，方便比對縣市名稱"""
    return (s or "").replace("台", "臺")


def parse_cap_time(s: str) -> datetime | None:
    """'2026/10/22 下午 08:15:00' → aware datetime（台灣時間）"""
    s = (s or "").strip()
    m = re.match(r"(\d{4})/(\d{1,2})/(\d{1,2})\s*(上午|下午)?\s*(\d{1,2}):(\d{2}):(\d{2})", s)
    if not m:
        try:
            return datetime.fromisoformat(s).astimezone(TW)
        except ValueError:
            return None
    y, mo, d, ampm, h, mi, se = m.groups()
    h = int(h)
    if ampm == "下午" and h != 12:
        h += 12
    if ampm == "上午" and h == 12:
        h = 0
    return datetime(int(y), int(mo), int(d), h, int(mi), int(se), tzinfo=TW)


def fmt(dt: datetime | None) -> str:
    return dt.strftime("%m/%d %H:%M") if dt else "—"


# ---------------------------------------------------------------- 抓資料
def fetch_feed() -> str:
    path = env("NCDR_FEED_FILE")
    if path:
        with open(path, encoding="utf-8") as f:
            return f.read()
    req = urllib.request.Request(FEED_URL, headers={"User-Agent": "site-weather-alert/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8")


def parse_feed(xml_text: str) -> list[dict]:
    root = ET.fromstring(xml_text)
    out = []
    for e in root.findall("a:entry", NS):
        link = e.find("a:link", NS)
        cat = e.find("a:category", NS)
        out.append({
            "id": (e.findtext("a:id", "", NS) or "").strip(),
            "title": (e.findtext("a:title", "", NS) or "").strip(),
            "category": cat.get("term", "") if cat is not None else "",
            "updated": datetime.fromisoformat(e.findtext("a:updated", "", NS)).astimezone(TW),
            "author": e.findtext("a:author/a:name", "", NS) or e.findtext("a:author/a:n", "", NS),
            "link": link.get("href", "") if link is not None else "",
            "summary": (e.findtext("a:summary", "", NS) or "").strip(),
            "msgType": e.findtext("cap:msgType", "", NS),
            "effective": parse_cap_time(e.findtext("cap:effective", "", NS)),
            "expires": parse_cap_time(e.findtext("cap:expires", "", NS)),
        })
    out.sort(key=lambda x: x["updated"], reverse=True)
    return out


def latest_active(entries: list[dict], category: str, now: datetime) -> dict | None:
    """該類別最新一筆；已過期或是 Cancel 就視為沒有示警"""
    for e in entries:
        if e["category"] == category or e["title"] == category:
            if e["msgType"] == "Cancel":
                return None
            if e["expires"] and e["expires"] < now:
                return None
            return e
    return None


# ---------------------------------------------------------------- 解析各類示警
def counties_by_light(text: str) -> dict[str, str]:
    """把『…新北市、桃園市…局部地區…(橙色燈號)，基隆市…(黃色燈號)』拆成 燈號→該段縣市文字"""
    result: dict[str, str] = {}
    for seg, color in re.findall(r"([^。]*?)\((紅|橙|黃)色燈號\)", norm(text)):
        seg = re.split(r"[，。]", seg)[-1]          # 只留這個燈號前面那一段縣市清單
        seg = re.sub(r"局部地區.*$", "", seg)       # 去掉「局部地區有平均風…」
        key = {"紅": "red", "橙": "orange", "黃": "yellow"}[color]
        result[key] = (result.get(key, "") + "、" + seg).strip("、")
    return result


def site_light_level(text: str, sites: list[str]) -> tuple[str, dict]:
    by = counties_by_light(text)
    level = "none"
    for lv, seg in by.items():
        if any(s in seg for s in sites) and LEVEL_RANK[lv] > LEVEL_RANK[level]:
            level = lv
    return level, by


def analyze_typhoon(e: dict, sites: list[str]) -> dict:
    s = e["summary"]
    kind = "陸上" if "LAND" in s[:20] else "海上"
    m = re.search(r"(輕度|中度|強烈)颱風\s", s)
    text = s[m.start():] if m else s
    name = re.search(r"(輕度|中度|強烈)颱風\s*(\S+?)（國際命名\s*(\w+)）", text)
    no = re.match(r"^([\d\-]+)", s)
    land = re.search(r"陸上：([^。]*?)應嚴加戒備", text)
    land_areas = norm(land.group(1)) if land else ""
    site_hit = any(x.replace("市", "").replace("縣", "") in land_areas for x in sites)
    level, by = site_light_level(text, sites)
    pos = re.search(r"中心位置在([^，]*，[^。]*?之處)", text)
    wind = re.search(r"近中心最大風速每秒\s*(\d+)\s*公尺[^，]*，相當於\s*(\d+)\s*級風", text)
    gust = re.search(r"瞬間最大陣風每秒\s*(\d+)\s*公尺[^，]*，相當於\s*([^，]+?級風[^，]*)", text)
    r7 = re.search(r"七級風暴風半徑\s*(\d+)\s*公里", text)
    move = re.search(r"以每小時([^，]+?公里)速度，向([^，]+?)進行", text)
    severity = "red" if (kind == "陸上" and site_hit) else ("orange" if kind == "陸上" else "blue")
    return {
        "kind": kind, "site_hit": site_hit, "land_areas": land_areas, "light": level,
        "lights": by, "severity": severity,
        "name": f"{name.group(1)}颱風 {name.group(2)}（{name.group(3)}）" if name else "颱風",
        "no": no.group(1) if no else "",
        "position": pos.group(1) if pos else "",
        "wind": f"每秒 {wind.group(1)} 公尺（{wind.group(2)} 級）" if wind else "",
        "gust": f"每秒 {gust.group(1)} 公尺（{gust.group(2)}）" if gust else "",
        "r7": f"{r7.group(1)} 公里" if r7 else "",
        "move": f"向{move.group(2)}，時速{move.group(1)}" if move else "",
        "text": text,
    }


def analyze_wind(e: dict, sites: list[str]) -> dict:
    level, by = site_light_level(e["summary"], sites)
    return {"light": level, "lights": by, "severity": level if level != "none" else "grey"}


def analyze_rain(e: dict, keywords: list[str]) -> dict:
    s = norm(e["summary"])
    hit = [k for k in keywords if norm(k) in s]
    grade = max((g for g in RAIN_RANK if g in s), key=lambda g: RAIN_RANK[g], default="")
    sev = "none"
    if hit:
        sev = {"大雨": "yellow", "豪雨": "orange", "大豪雨": "red", "超大豪雨": "red"}.get(grade, "yellow")
    return {"hit": hit, "grade": grade, "severity": sev if sev != "none" else "grey"}


# ---------------------------------------------------------------- 決定要不要推
def signature(cat: str, info: dict) -> str:
    if cat == "颱風":
        return f"{info['kind']}|{info['site_hit']}|{info['light']}"
    if cat == "強風":
        return info["light"]
    if cat == "降雨":
        return f"{bool(info['hit'])}|{info['grade'] if info['hit'] else ''}"
    return "on"


def is_relevant(cat: str, info: dict) -> bool:
    """跟工地有沒有關係"""
    if cat == "颱風":
        return True  # 有颱風警報就通報（海上警報也先讓大家知道）
    if cat == "強風":
        return info["light"] != "none"
    if cat == "降雨":
        return bool(info["hit"])
    return True


def is_severe(cat: str, info: dict) -> bool:
    """安靜時段也要發的等級"""
    if cat == "颱風":
        return info["kind"] == "陸上" and info["site_hit"]
    if cat == "強風":
        return LEVEL_RANK[info["light"]] >= 2
    if cat == "降雨":
        return RAIN_RANK.get(info["grade"], 0) >= 2 and bool(info["hit"])
    return False


def in_quiet(now: datetime) -> bool:
    qs, qe = int(env("QUIET_START", "19")), int(env("QUIET_END", "7"))
    h = now.hour
    return (h >= qs or h < qe) if qs > qe else (qs <= h < qe)


# ---------------------------------------------------------------- Discord 訊息
def build_embed(cat: str, e: dict, info: dict, change: str) -> dict:
    sev = info["severity"]
    fields = []
    if cat == "颱風":
        title = f"🌀 {info['kind']}颱風警報｜{info['name']}"
        if info["site_hit"]:
            title = "🚨 " + title + "｜工地在警戒區"
        desc_lines = [x for x in [
            f"**位置**：{info['position']}" if info["position"] else "",
            f"**強度**：近中心最大風速 {info['wind']}，陣風 {info['gust']}" if info["wind"] else "",
            f"**七級風暴風半徑**：{info['r7']}" if info["r7"] else "",
            f"**動態**：{info['move']}" if info["move"] else "",
        ] if x]
        if info["land_areas"]:
            fields.append({"name": "陸上警戒區域", "value": info["land_areas"][:1000], "inline": False})
        if info["lights"]:
            fields.append({"name": "工地縣市強風燈號", "value": LEVEL_NAME[info["light"]], "inline": True})
        desc = "\n".join(desc_lines)
    elif cat == "強風":
        title = f"💨 強風特報｜工地縣市 {LEVEL_NAME[info['light']]}"
        desc = e["summary"][:1500]
    elif cat == "降雨":
        title = f"🌧️ {info['grade'] or '降雨'}特報｜{'、'.join(info['hit'])}"
        desc = e["summary"][:1500]
    else:
        title = f"⚠️ {cat}"
        desc = e["summary"][:1500]

    fields.append({"name": "有效時間", "value": f"{fmt(e['effective'])} ～ {fmt(e['expires'])}", "inline": True})
    fields.append({"name": "發布", "value": f"{e['author']}　{fmt(e['updated'])}"
                   + (f"　第{info['no']}報" if cat == "颱風" and info.get("no") else ""), "inline": True})
    actions = SITE_ACTIONS.get(cat)
    if actions:
        fields.append({"name": "🏗️ 工地應變", "value": "\n".join(f"☐ {a}" for a in actions), "inline": False})
    links = f"[CAP 原文]({e['link']})　[NCDR 颱風監測]({WATCH_URL})"
    fields.append({"name": "連結", "value": links, "inline": False})
    return {
        "title": title[:250],
        "description": (f"**{change}**\n" if change else "") + desc,
        "color": COLOR.get(sev, COLOR["grey"]),
        "fields": fields,
        "footer": {"text": "資料來源：中央氣象署／NCDR 災害示警公開資料平台"},
        "timestamp": e["updated"].isoformat(),
    }


def build_clear_embed(cat: str, now: datetime) -> dict:
    return {
        "title": f"✅ {cat}示警解除（工地縣市）",
        "description": "目前已無生效中的示警，復工前請巡查鷹架、圍籬、吊掛設備與排水。",
        "color": COLOR["green"],
        "timestamp": now.isoformat(),
    }


def send_discord(embeds: list[dict]) -> None:
    url = env("DISCORD_WEBHOOK_URL")
    mention = env("DISCORD_MENTION")
    for i in range(0, len(embeds), 10):  # Discord 一則最多 10 個 embed
        payload = {"username": "工地天氣警示", "embeds": embeds[i:i + 10]}
        if mention and i == 0:
            payload["content"] = mention
            payload["allowed_mentions"] = {"parse": ["everyone", "roles", "users"]}
        if env("DRY_RUN") == "1" or not url:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            continue
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json",
                                              "User-Agent": "site-weather-alert/1.0"})
        urllib.request.urlopen(req, timeout=30)


# ---------------------------------------------------------------- 主流程
def load_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(path: str, state: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def run(now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(TW)
    sites = [norm(s) for s in env("SITE_COUNTIES", "臺北市,新北市").split(",") if s.strip()]
    rain_kw = [s for s in env("RAIN_KEYWORDS", ",".join(sites + ["北部", "桃園以北", "北北基", "雙北"])).split(",") if s.strip()]
    cats = [c for c in env("CATEGORIES", "颱風,強風,降雨").split(",") if c.strip()]
    remind_h = float(env("TYPHOON_REMIND_HOURS", "3"))
    state_path = env("STATE_FILE", "weather_alert/state.json")
    force = env("FORCE") == "1"

    entries = parse_feed(fetch_feed())
    state = load_state(state_path)
    quiet = in_quiet(now) and not force
    embeds: list[dict] = []

    for cat in cats:
        prev = state.get(cat, {})
        e = latest_active(entries, cat, now)
        info = None
        if e:
            if cat == "颱風":
                info = analyze_typhoon(e, sites)
            elif cat == "強風":
                info = analyze_wind(e, sites)
            elif cat == "降雨":
                info = analyze_rain(e, rain_kw)
            else:
                info = {"severity": "orange"}

        relevant = bool(e) and is_relevant(cat, info)

        # 示警結束 → 發解除通知
        if not relevant:
            if prev.get("active") and not quiet:
                embeds.append(build_clear_embed(cat, now))
                state[cat] = {"active": False, "sig": "", "last_push": now.isoformat()}
            elif prev.get("active") and quiet:
                pass  # 解除訊息留到早上再發
            else:
                state[cat] = {"active": False, "sig": "", "last_push": prev.get("last_push", "")}
            continue

        sig = signature(cat, info)
        last_push = datetime.fromisoformat(prev["last_push"]) if prev.get("last_push") else None
        changed = sig != prev.get("sig")
        remind = (cat == "颱風" and last_push is not None
                  and now - last_push >= timedelta(hours=remind_h)
                  and e["id"] != prev.get("last_id"))

        if not (changed or remind or force):
            continue
        if quiet and not is_severe(cat, info):
            continue  # 安靜時段不發，狀態不更新，早上 7 點後自然補發

        if not prev.get("active"):
            change = "新發布"
        elif changed:
            change = "狀態變更"
        else:
            change = f"持續中（每 {remind_h:g} 小時提醒）"
        embeds.append(build_embed(cat, e, info, change))
        state[cat] = {"active": True, "sig": sig, "last_push": now.isoformat(), "last_id": e["id"]}

    if embeds:
        send_discord(embeds)
    save_state(state_path, state)
    print(f"[{now:%Y-%m-%d %H:%M}] 檢查完成，推播 {len(embeds)} 則，安靜時段={quiet}")
    return embeds


if __name__ == "__main__":
    try:
        run()
    except Exception as ex:  # 失敗也通知，避免默默壞掉
        print(f"錯誤：{ex}", file=sys.stderr)
        if env("DISCORD_WEBHOOK_URL") and env("DRY_RUN") != "1" and env("NOTIFY_ON_ERROR", "1") == "1":
            try:
                send_discord([{"title": "⚠️ 天氣警示程式執行失敗", "description": str(ex)[:1500],
                               "color": COLOR["grey"]}])
            except Exception:
                pass
        sys.exit(1)
