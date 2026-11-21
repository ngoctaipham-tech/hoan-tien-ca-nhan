#!/usr/bin/env python3
"""Cập nhật data/promos.json từ trang ưu đãi CHÍNH THỨC của các ngân hàng.

- Mỗi ngân hàng một hàm fetch_*; lỗi ở 1 ngân hàng không làm hỏng cả lượt chạy:
  giữ lại ưu đãi cũ (còn hạn) của ngân hàng đó và ghi lỗi vào data/update_status.json.
- Chỉ giữ ưu đãi còn hạn (valid_to >= hôm nay, giờ Việt Nam), loại trùng.
- Ngân hàng chặn request thường (MB, VIB, HDBank, VietinBank) được đọc qua Chromium (Playwright).

Chạy: python scripts/update_promos.py [--only vpbank,tpbank] [--no-browser]
"""
import argparse, datetime as dt, hashlib, html, io, json, os, pathlib, re, sys, time, traceback

import requests

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
VN = dt.timezone(dt.timedelta(hours=7))
NOW = dt.datetime.now(VN)
TODAY = NOW.date()
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
S = requests.Session()
S.headers.update({"User-Agent": UA, "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.5"})
MAX_DETAIL = int(os.environ.get("MAX_DETAIL", "15"))


class BankError(Exception):
    pass


# ---------------------------------------------------------------- helpers
def log(*a):
    print(*a, file=sys.stderr, flush=True)


def get(url, **kw):
    r = S.get(url, timeout=kw.pop("timeout", 40), **kw)
    r.raise_for_status()
    return r


def strip_html(s):
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def pdate(s):
    """dd/mm/yyyy | dd.mm.yyyy | yyyy-mm-dd[T..] -> 'YYYY-MM-DD' or None"""
    if not s:
        return None
    s = str(s).strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return f"{m[1]}-{m[2]}-{m[3]}"
    m = re.search(r"(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})", s)
    if m:
        try:
            return dt.date(int(m[3]), int(m[2]), int(m[1])).isoformat()
        except ValueError:
            return None
    return None


DATE = r"\d{1,2}[/.]\d{1,2}[/.]\d{4}"


def dates_from_text(t):
    """Tìm (từ, đến) trong văn bản chi tiết ưu đãi."""
    t = t or ""
    pats = [
        rf"[Hh][Ếế][Tt]\s*[Hh][Ạạ][Nn](?:\s*[Nn]gày)?\s*:?\s*({DATE})",
        rf"({DATE})\s*(?:-|–|đến|tới)\s*(?:ngày\s*)?({DATE})",
        rf"[Tt]ừ\s*(?:ngày\s*)?({DATE})\s*(?:-|–|đến|tới)\s*(?:hết\s*)?(?:ngày\s*)?({DATE})",
        rf"(?:[Tt]ừ nay|[Tt]ừ bây giờ)\s*(?:đến|tới)\s*(?:hết\s*)?(?:ngày\s*)?({DATE})",
        rf"[Đđ]ến\s*(?:hết\s*)?ngày\s*({DATE})",
    ]
    for i, p in enumerate(pats):
        m = re.search(p, t)
        if m:
            g = m.groups()
            if len(g) == 2:
                return pdate(g[0]), pdate(g[1])
            return None, pdate(g[0])
    return None, None


CAT_RULES = [
    ("shopee", ["shopee"]),
    ("ve_may_bay", ["vé máy bay", "hàng không", "vietnam airlines", "vietjet", "bamboo airways", "vietravel airlines", "chuyến bay", "airlines", "airways"]),
    ("khach_san", ["khách sạn", "resort", "hotel", "agoda", "booking.com", "villa", "lưu trú"]),
    ("du_lich", ["du lịch", "traveloka", "klook", "tour", "phòng chờ", "sân bay", "lounge", "trip.com", "fast track", "visa travel"]),
    ("an_uong", ["nhà hàng", "ẩm thực", "ăn uống", "cafe", "cà phê", "coffee", "highlands", "starbucks", "phúc long", "golden gate", "pizza", "lẩu", "nướng", "buffet", "kfc", "lotteria", "jollibee", "grabfood", "shopeefood", "food", "dining", "steak", "sushi", "trà sữa", "bánh", "the coffee house", "katinat", "manwah", "gogi", "haidilao", "michelin", "bar "]),
    ("sieu_thi", ["siêu thị", "winmart", "co.op", "coopmart", "lotte mart", "go!", "big c", "aeon", "emart", "bách hóa", "mm mega", "circle k", "gs25", "7-eleven", "familymart", "ministop", "tops market"]),
    ("xang_dau", ["xăng", "petrolimex", "nhiên liệu", "trạm sạc", "vinfast charging"]),
    ("mua_online", ["lazada", "tiki", "tiktok shop", "thương mại điện tử", "online", "trực tuyến", "sendo", "website", "app "]),
    ("giao_duc", ["giáo dục", "học phí", "trường", "edu", "khóa học", "anh ngữ", "ila", "vus", "apollo", "fahasa"]),
    ("y_te", ["bệnh viện", "nha khoa", "y tế", "phòng khám", "pharmacity", "long châu", "an khang", "sức khỏe", "vinmec", "thẩm mỹ"]),
    ("hoa_don", ["hóa đơn điện", "tiền điện", "tiền nước", "cước", "viễn thông", "internet", "thanh toán hóa đơn", "bảo hiểm"]),
    ("giai_tri", ["cgv", "lotte cinema", "galaxy", "bhd", "rạp", "phim", "vé xem", "concert", "giải trí", "golf", "netflix", "spotify", "game", "vinwonders", "sun world", "karaoke", "bowling"]),
    ("tra_gop", ["trả góp", "0% lãi", "lãi suất 0%", "installment"]),
]


def classify(text):
    t = " " + (text or "").lower() + " "
    if "shopeefood" in t or "shopee food" in t:
        return "an_uong"
    for cat, kws in CAT_RULES:
        if any(k in t for k in kws):
            return cat
    return "khac"


def norm(s):
    return re.sub(r"[^0-9a-zà-ỹđ]+", " ", (s or "").lower()).strip()


def mkid(bank, url, title):
    return bank + "-" + hashlib.sha1((url or title).encode()).hexdigest()[:10]


# ---------------------------------------------------------------- browser
class Browser:
    def __init__(self, enabled=True):
        self.enabled = enabled
        self._pw = self._b = self._ctx = None

    def ctx(self):
        if not self.enabled:
            raise BankError("cần trình duyệt nhưng đã tắt (--no-browser)")
        if self._ctx is None:
            try:
                from playwright.sync_api import sync_playwright
            except Exception as e:
                raise BankError(f"không có playwright: {e}")
            self._pw = sync_playwright().start()
            exe = os.environ.get("CHROME_PATH")
            kw = {"args": ["--no-sandbox", "--disable-blink-features=AutomationControlled"]}
            if exe:
                kw["executable_path"] = exe
            self._b = self._pw.chromium.launch(**kw)
            self._ctx = self._b.new_context(user_agent=UA, locale="vi-VN", viewport={"width": 1366, "height": 900})
        return self._ctx

    def page(self):
        return self.ctx().new_page()

    def goto(self, pg, url, wait_ms=5000):
        pg.goto(url, wait_until="domcontentloaded", timeout=90000)
        pg.wait_for_timeout(wait_ms)

    def text(self, url, wait_ms=5000):
        pg = self.page()
        try:
            self.goto(pg, url, wait_ms)
            return pg.inner_text("body")
        finally:
            pg.close()

    def anchors(self, url, wait_ms=7000, scroll=True):
        pg = self.page()
        try:
            self.goto(pg, url, wait_ms)
            if scroll:
                for _ in range(6):
                    pg.mouse.wheel(0, 2500)
                    pg.wait_for_timeout(700)
            return pg.eval_on_selector_all("a[href]", "els=>els.map(e=>({h:e.href,t:(e.innerText||'').trim()}))")
        finally:
            pg.close()

    def close(self):
        try:
            if self._b:
                self._b.close()
            if self._pw:
                self._pw.stop()
        except Exception:
            pass


BR = None


# ---------------------------------------------------------------- banks
def fetch_vpbank():
    out, seen = [], set()
    base = "https://www.vpbank.com.vn/uiux-api/api/promotion"
    for page in range(1, 41):  # 6/trang
        r = get(base, params={"pageSize": 6, "pageIndex": page, "lang": "vi",
                              "categoryPath": "/uu-dai/retail/for-cardholder-promotion-category"})
        j = r.json()
        items = j.get("items") or j.get("data") or j.get("results") or []
        if isinstance(items, dict):
            items = items.get("items") or []
        if not items:
            break
        for it in items:
            path = it.get("path") or ""
            if path in seen:
                continue
            seen.add(path)
            out.append({
                "title": (it.get("title") or "").strip(),
                "url": "https://www.vpbank.com.vn" + path if path.startswith("/") else path,
                "valid_from": pdate(it.get("fromDate")), "valid_to": pdate(it.get("toDate")),
                "merchant": None, "desc": strip_html(it.get("description") or it.get("summary") or ""),
                "extra": " ".join(filter(None, [it.get("categoryTitle"), strip_html(it.get("address") or "")])),
            })
        # danh sách sắp theo mới nhất; dừng khi cả trang đã hết hạn quá lâu
        tos = [pdate(i.get("toDate")) for i in items]
        if tos and all(t and t < (TODAY - dt.timedelta(days=200)).isoformat() for t in tos):
            break
    return out


def fetch_techcombank():
    url = ("https://techcombank.com/content/techcombank/web/vn/vi/khach-hang-ca-nhan/uu-dai/jcr:content/root/"
           "container/sectioncontainer/offerlisting.offerlisting.json")
    j = get(url, params={"limit": 0, "offset": 0, "sort": "most popular"}).json()
    res = j.get("results") or j.get("items") or []
    out = []
    for it in res:
        name = it.get("name") or ""
        types = it.get("dataCardTypes") or it.get("products") or []
        if isinstance(types, str):
            types = [types]
        partners = it.get("partner") or []
        if isinstance(partners, str):
            partners = [partners]
        desc = strip_html(it.get("description") or "")
        out.append({
            "title": (it.get("title") or desc[:120] or name).strip(),
            "url": f"https://techcombank.com/khach-hang-ca-nhan/uu-dai/{name}",
            "valid_from": pdate(it.get("publishedDate")) if it.get("publishedDate") else None,
            "valid_to": pdate(it.get("expiryDate")),
            "merchant": ", ".join(p if isinstance(p, str) else (p.get("title") or p.get("name") or "") for p in partners) or None,
            "desc": desc, "extra": " ".join(map(str, it.get("categories") or [])),
            "tcb_types": [t if isinstance(t, str) else json.dumps(t) for t in types],
        })
    return out


def fetch_tpbank():
    url = ("https://tpb.vn/wps/wcm/connect/tpbank_data_promotion/sa-website/sa-promotion/vn"
           "?source=library&srv=cmpnt&cmpntid=4dbd1e01-efa8-429b-8422-81df65566470")
    s = get(url, timeout=90).text
    out = []
    parts = re.split(r'<div[^>]+class="[^"]*km-content-item[^"]*"', s)[1:]
    heads = re.findall(r'<div[^>]+class="[^"]*km-content-item[^"]*"([^>]*)>', s)
    for head, body in zip(heads, parts):
        path = re.search(r'path="([^"]+)"', head)
        path = path[1] if path else ""
        if "/vn/the-tin-dung/" not in path and "/vn/the/" not in path:
            continue  # chỉ lấy ưu đãi thẻ tín dụng
        slug = path.rstrip("/").split("/")[-1]
        body_txt = strip_html(body[:6000])
        title = re.search(r"<h3[^>]*>(.*?)</h3>", body, re.S)
        title = strip_html(title[1]) if title else slug
        partner = re.search(r"Đối tác:\s*(.*?)\s*(?:Sản phẩm:|Địa điểm:|Thời gian:|$)", body_txt)
        place = re.search(r"Địa điểm:\s*(.*?)\s*(?:Thời gian:|Xem chi tiết|$)", body_txt)
        tf = re.search(r'data-timestart="([^"]*)"', head)
        tt = re.search(r'data-timecountdown="([^"]*)"', head)
        out.append({
            "title": title, "url": f"https://tpb.vn/khuyen-mai/the-tin-dung/{slug}",
            "valid_from": pdate(tf[1]) if tf else None, "valid_to": pdate(tt[1]) if tt else None,
            "merchant": (partner[1].strip() or None) if partner else None,
            "desc": (place[1].strip()[:300] if place else ""), "extra": "thẻ tín dụng",
        })
    if not out:
        raise BankError("không tìm thấy mục km-content-item")
    return out


def fetch_mbbank():
    ctx = BR.ctx()
    pg = ctx.new_page()
    captured = {}

    def on_req(req):
        if "posts/get-all" in req.url and req.method == "POST" and "req" not in captured:
            captured["req"] = req.post_data
            captured["headers"] = req.headers
    pg.on("request", on_req)
    try:
        BR.goto(pg, "https://www.mbbank.com.vn/ca-nhan/khach-hang-ca-nhan/uu-dai", 9000)
        if "req" not in captured:
            BR.goto(pg, "https://www.mbbank.com.vn/ca-nhan/khach-hang-ca-nhan/uu-dai/the", 9000)
        if "req" not in captured:
            raise BankError("không bắt được API posts/get-all (có thể bị chặn)")
        body = json.loads(captured["req"])
        body["size"] = 60
        body["page"] = 1
        hdr = {k: v for k, v in captured["headers"].items() if not k.startswith(":") and k.lower() not in ("content-length", "cookie", "host")}
        resp = pg.evaluate("""async ([b,h])=>{const r=await fetch('https://www.mbbank.com.vn/mb-website/v1/posts/get-all',
            {method:'POST',headers:h,body:JSON.stringify(b),credentials:'include'});return await r.text();}""", [body, hdr])
        j = json.loads(resp)
        content = j.get("content") or []
        if not content:
            raise BankError("API MB trả về rỗng")
        cand = []
        for c in content:
            cat = c.get("category") or {}
            cslug = ((cat.get("categoryTranslations") or [{}])[0]).get("slug") or ""
            if cslug not in ("the", "uu-dai"):
                continue
            tr = (c.get("postTranslations") or [{}])[0]
            title, slug = (tr.get("title") or "").strip(), tr.get("slug") or ""
            if not slug or "biểu phí" in title.lower() or "cẩm nang" in title.lower():
                continue
            cand.append({"title": title, "url": f"https://www.mbbank.com.vn/ca-nhan/khach-hang-ca-nhan/uu-dai/{cslug}/{slug}",
                         "valid_from": pdate(c.get("startViewDate") or c.get("startDate")),
                         "valid_to": pdate(c.get("endViewDate") or c.get("endDate")),
                         "merchant": None, "desc": strip_html(tr.get("description") or ""), "extra": ""})
        out = []
        for it in cand[:MAX_DETAIL * 2]:
            if len([x for x in out if x.get("_detail")]) >= MAX_DETAIL:
                break
            try:
                BR.goto(pg, it["url"], 4500)
                t = pg.inner_text("body")
                it["_detail"] = True
                f, to = dates_from_text(t)
                it["valid_from"] = it["valid_from"] or f
                it["valid_to"] = it["valid_to"] or to
                if not it["desc"]:
                    i = t.find(it["title"][:30])
                    it["desc"] = re.sub(r"\s+", " ", t[i + len(it["title"]):i + len(it["title"]) + 400] if i >= 0 else "")[:300]
                it["extra"] = t[:3000].lower()
            except Exception as e:
                log("  mb detail fail", it["url"], e)
            out.append(it)
        return out
    finally:
        pg.close()


def fetch_vib():
    ctx = BR.ctx()
    pg = ctx.new_page()
    captured = {}

    def on_req(req):
        if "promotion/api/search" in req.url and "req" not in captured:
            captured["req"] = req.post_data
    pg.on("request", on_req)
    try:
        BR.goto(pg, "https://www.vib.com.vn/vn/uu-dai", 10000)
        items = []
        if captured.get("req"):
            body = json.loads(captured["req"])
            body.update({"pageSize": 80, "pageNum": 1})
            resp = pg.evaluate("""async (b)=>{const r=await fetch('/promotion/api/search',{method:'POST',
                headers:{'content-type':'application/json','accept':'application/json','x-requested-with':'XMLHttpRequest'},
                body:JSON.stringify(b)});return await r.text();}""", body)
            try:
                items = (json.loads(resp).get("data") or {}).get("cardPromotionModelList") or []
            except Exception:
                items = []
        out = []
        for it in items:
            pid = it.get("proId") or it.get("nameCms")
            if not pid:
                continue
            out.append({"title": (it.get("proNameVn") or "").strip(),
                        "url": f"https://www.vib.com.vn/vn/promotion/details/{pid}",
                        "valid_from": pdate(it.get("effectDate")), "valid_to": pdate(it.get("expireDate")),
                        "merchant": None, "desc": strip_html(it.get("descriptionVN") or ""), "extra": ""})
        if out:
            return out
        # dự phòng: đọc thẻ <a> trên trang
        links = pg.eval_on_selector_all("a[href*='/promotion/details/']", "els=>els.map(e=>({h:e.href,t:(e.innerText||'').trim()}))")
        for a in links:
            lines = [l.strip() for l in a["t"].split("\n") if l.strip()]
            if not lines:
                continue
            to = None
            for l in lines:
                m = re.search(r"Ngày hết hạn:\s*(" + DATE + ")", l)
                if m:
                    to = pdate(m[1])
                m = re.search(r"Chỉ còn\s*(\d+)\s*ngày", l)
                if m:
                    to = (TODAY + dt.timedelta(days=int(m[1]))).isoformat()
            out.append({"title": lines[0], "url": a["h"], "valid_from": None, "valid_to": to, "merchant": None, "desc": "", "extra": ""})
        if not out:
            raise BankError("không đọc được danh sách ưu đãi VIB (trang thử thách/chặn?)")
        return out
    finally:
        pg.close()


def fetch_hdbank():
    links = []
    for wait in (9000, 18000):  # trang đôi khi tải chậm → thử lại 1 lần
        links = BR.anchors("https://hdbank.com.vn/vi/personal/promotion", wait)
        if any("/promotion/detail/the/" in a["h"] for a in links):
            break
    cand, seen = [], set()
    for a in links:
        h = a["h"].split("?")[0]
        if "/promotion/detail/the/" not in h or h in seen:
            continue
        seen.add(h)
        lines = [l.strip() for l in a["t"].split("\n") if l.strip()]
        title = max(lines, key=len) if lines else h.rsplit("/", 1)[-1].replace("-", " ")
        cand.append({"title": title, "url": h, "valid_from": None, "valid_to": None, "merchant": None, "desc": "", "extra": ""})
    if not cand:
        raise BankError("không thấy liên kết ưu đãi thẻ trên trang HDBank")
    pg = BR.page()
    out = []
    try:
        for it in cand[:MAX_DETAIL]:
            try:
                BR.goto(pg, it["url"], 4000)
                t = pg.inner_text("body")
                h1 = pg.eval_on_selector("h1", "e=>e.innerText") if pg.query_selector("h1") else ""
                if h1 and len(h1.strip()) > 8:
                    it["title"] = h1.strip()
                # chỉ xét phần đầu trang (tránh dính ngày của ưu đãi liên quan phía dưới)
                j = t.find(h1.strip()[:30]) if h1 else -1
                body = t[j:j + 1400] if j >= 0 else t[:1400]
                i = body.find("Thời gian ưu đãi")
                f, to = dates_from_text(body[i:i + 300] if i >= 0 else body)
                it["valid_from"], it["valid_to"] = f, to
                m = re.search(r"(Từ nay đến[^\n]{0,300}|Giảm[^\n]{0,250}|Trả góp[^\n]{0,250})", body[:1500])
                it["desc"] = m[1][:300] if m else ""
                it["extra"] = body.lower()
            except Exception as e:
                log("  hdb detail fail", it["url"], e)
            out.append(it)
    finally:
        pg.close()
    return out


def fetch_vietinbank():
    cand, seen = [], set()
    for src in ("https://www.vietinbank.vn/ca-nhan/uu-dai/chuong-trinh-khuyen-mai",
                "https://www.vietinbank.vn/ca-nhan/uu-dai/the-gioi-uu-dai"):
        try:
            links = BR.anchors(src, 9000)
        except Exception as e:
            log("  ctg list fail", src, e)
            continue
        for a in links:
            h = a["h"].split("?")[0]
            if "/ca-nhan/uu-dai/" not in h or not h.endswith("-10-html") or h in seen:
                continue
            lines = [l.strip() for l in a["t"].split("\n") if l.strip()]
            if not lines or any(l.lower().startswith("hết hạn") for l in lines):
                continue
            seen.add(h)
            to = None
            for l in lines:
                m = re.search(r"Còn\s*(\d+)\s*ngày", l)
                if m:
                    to = (TODAY + dt.timedelta(days=int(m[1]))).isoformat()
            title = max(lines, key=len)
            cand.append({"title": title, "url": h, "valid_from": None, "valid_to": to, "merchant": None, "desc": "", "extra": ""})
    if not cand:
        raise BankError("không thấy ưu đãi trên trang VietinBank")
    # đọc chi tiết để lấy thời gian & đối tượng thẻ
    pg = BR.page()
    try:
        for it in cand[:MAX_DETAIL]:
            try:
                BR.goto(pg, it["url"], 3500)
                t = pg.inner_text("body")
                i = t.find(it["title"][:25])
                seg = t[i:i + 4000] if i >= 0 else t[:4000]
                f, to = dates_from_text(seg)
                it["valid_from"] = f or it["valid_from"]
                it["valid_to"] = to or it["valid_to"]
                it["extra"] = seg.lower()
                m = re.search(r"((?:Giảm|Hoàn|Tặng|Ưu đãi)[^\n]{10,250})", seg)
                it["desc"] = m[1][:300] if m else ""
            except Exception as e:
                log("  ctg detail fail", it["url"], e)
    finally:
        pg.close()
    return cand


PDF_FIX = {"đ÷ng": "đồng", "÷": "ồ", "quõc": "quốc", "Đõi": "Đối", "đõi": "đối", "Sõ ": "Số ", "Nöi": "Nội", "nöi": "nội"}


def fix_pdf_text(t):
    t = re.sub(r"\s+", " ", t or "")
    for a, b in PDF_FIX.items():
        t = t.replace(a, b)
    # pypdf hay chèn khoảng trắng giữa phụ âm và vần: "đ ến", "th ể"
    t = re.sub(r"\b(đ|th|d|tr|ch|ng|nh|kh|ph|h|m|n|t|c|b|l|v|s|x|r|g|k) (ế|ể|ầ|ụ|ị|ữ|ồ|ư|ơ|ố|ạ|ả|ặ)", r"\1\2", t)
    return t


def fetch_vikki():
    s = get("https://vikkibank.vn/uu-dai-the/").text
    pdfs = []
    for m in re.finditer(r'href=["\'](https://vikkibank\.vn/wp-content/uploads/[^"\']+\.pdf)["\']', s):
        u = m[1]
        name = u.rsplit("/", 1)[-1].lower()
        if u in pdfs or "dieu-khoan" in name or "bieu-phi" in name:
            continue
        pdfs.append(u)
    try:
        from pypdf import PdfReader
    except Exception as e:
        raise BankError(f"thiếu pypdf: {e}")
    out = []
    for u in pdfs[:20]:
        try:
            b = get(u, timeout=60).content
            flat = fix_pdf_text(" ".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(b)).pages[:2]))
        except Exception as e:
            log("  vikki pdf fail", u, e)
            continue
        if "THỂ LỆ" not in flat.upper()[:400] and "CHƯƠNG TRÌNH" not in flat.upper()[:400]:
            continue
        m = re.search(r"Tên chương trình[^:]{0,20}:\s*(.{8,200}?)\.\s+\d+\.", flat) or \
            re.search(r"[“\"](.{8,150}?)[”\"]", flat[:400])
        title = m[1].strip(" :.-") if m else u.rsplit("/", 1)[-1][:-4].replace("-", " ")
        i = flat.find("Thời gian")
        f, to = dates_from_text(flat[i:i + 300] if i >= 0 else flat)
        md = re.search(r"(?:Nội dung (?:khuyến mại|ưu đãi)|Thể lệ chương trình)\s*:?\s*((?:Giảm|Hoàn|Tặng)[^;]{10,200}?)(?:\.\s|;|$)", flat) or \
            re.search(r"((?:Giảm|Hoàn|Tặng) [\d.]+[^;]{5,160}?)(?:\.\s|;|$)", flat)
        out.append({"title": title, "url": u, "valid_from": f, "valid_to": to, "merchant": None,
                    "desc": md[1].strip() if md else "", "extra": flat[:2500].lower()})
    if not out:
        raise BankError("không tìm thấy thể lệ ưu đãi (PDF) trên vikkibank.vn/uu-dai-the")
    return out


BANKS = {
    "vpbank": ("VPBank", fetch_vpbank, False),
    "techcombank": ("Techcombank", fetch_techcombank, False),
    "tpbank": ("TPBank", fetch_tpbank, False),
    "mbbank": ("MB", fetch_mbbank, True),
    "vib": ("VIB", fetch_vib, True),
    "vikki": ("Vikki Bank", fetch_vikki, False),
    "hdbank": ("HDBank", fetch_hdbank, True),
    "vietinbank": ("VietinBank", fetch_vietinbank, True),
}


# ---------------------------------------------------------------- mapping
def load_cards():
    return json.loads((DATA / "cards.json").read_text(encoding="utf-8"))["cards"]


def match_cards(bank_name, item, cards):
    text = " ".join([item.get("title") or "", item.get("desc") or "", item.get("merchant") or "", item.get("extra") or ""]).lower()
    ids = []
    for c in cards:
        if c["bank"] != bank_name:
            continue
        pm = c.get("promo_match") or {}
        if pm.get("tcb_card_type"):
            if any(pm["tcb_card_type"] in t for t in item.get("tcb_types") or []):
                ids.append(c["id"])
            continue
        title = (item.get("title") or "").lower()
        inc = pm.get("include_any") or []
        exc = pm.get("exclude_any") or []
        if inc and not any(k in text for k in inc):
            continue
        # loại trừ chỉ xét tiêu đề + mô tả ngắn (phần thân dài hay nhắc nhiều loại thẻ)
        head = title + " " + (item.get("desc") or "").lower()
        if any(k in head for k in exc):
            continue
        ids.append(c["id"])
    return ids


def discount_desc(item):
    rx = r"((?:[Gg]iảm|[Hh]oàn(?: tiền)?|[Tt]ặng|[Ưư]u đãi|[Mm]iễn phí|[Nn]hận|[Cc]hiết khấu|[Tt]rả góp)(?:[^;\n.]|\.(?=\d))+)"
    d = ""
    for src in (item.get("desc") or "", item.get("title") or ""):
        m = re.search(rx, src)
        if m:
            d = m[1]
            break
    d = d or item.get("desc") or item.get("title") or ""
    d = re.split(r"\s+Nguồn tin|\s{2,}", d)[0].strip()
    # bỏ phần lặp lại tiêu đề
    t = (item.get("title") or "").strip()
    if t and d.startswith(t) and len(d) > len(t) + 10:
        d = d[len(t):].strip(" .:-–")
    return d[:200].strip()


def finalize(bank_key, bank_name, raw, cards):
    out = []
    for it in raw:
        title = re.sub(r"\s+", " ", it.get("title") or "").strip()
        if not title or not it.get("url"):
            continue
        vt = it.get("valid_to")
        if vt and vt < TODAY.isoformat():
            continue
        if not vt and it.get("valid_from") and it["valid_from"] < (TODAY - dt.timedelta(days=365)).isoformat():
            continue  # không rõ hạn & quá cũ
        ids = match_cards(bank_name, it, cards)
        if not ids:
            continue
        out.append({
            "id": mkid(bank_key, it["url"], title), "bank": bank_name, "bank_key": bank_key, "card_ids": ids,
            "title_vi": title, "category": (lambda c: c if c != "khac" else classify(" ".join([it.get("merchant") or "", (it.get("desc") or "")[:160]])))(classify(title)),
            "merchant": it.get("merchant") or None, "discount_desc_vi": discount_desc(it),
            "valid_from": it.get("valid_from"), "valid_to": vt, "url": it["url"],
            "fetched_at": NOW.isoformat(timespec="seconds"),
        })
    return out


def dedup(items):
    by_id, by_title = {}, {}
    for p in items:
        if p["id"] in by_id:
            continue
        k = (p["bank"], norm(p["title_vi"]), p.get("valid_to"))
        if k in by_title:
            continue
        by_id[p["id"]] = p
        by_title[k] = p
    return list(by_id.values())


def main():
    global BR
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    only = [x for x in a.only.split(",") if x]
    cards = load_cards()
    pfile, sfile = DATA / "promos.json", DATA / "update_status.json"
    old = json.loads(pfile.read_text(encoding="utf-8")) if pfile.exists() else {"promos": []}
    old_status = json.loads(sfile.read_text(encoding="utf-8")) if sfile.exists() else {"banks": {}}
    BR = Browser(enabled=not a.no_browser)
    new_items, status = [], {}
    try:
        for key, (name, fn, needs_browser) in BANKS.items():
            prev = (old_status.get("banks") or {}).get(key, {})
            keep_old = [p for p in old.get("promos", []) if p.get("bank_key") == key and (not p.get("valid_to") or p["valid_to"] >= TODAY.isoformat())]
            if only and key not in only:
                new_items += keep_old
                status[key] = prev or {"bank": name, "status": "skipped"}
                continue
            t0 = time.time()
            log(f"== {name}")
            try:
                raw = fn()
                items = finalize(key, name, raw, cards)
                new_items += items
                status[key] = {"bank": name, "status": "ok", "raw": len(raw), "count": len(items),
                               "message": "", "last_success": NOW.isoformat(timespec="seconds"),
                               "seconds": round(time.time() - t0, 1), "via": "browser" if needs_browser else "http"}
                log(f"   ok raw={len(raw)} kept={len(items)}")
            except Exception as e:
                msg = f"{type(e).__name__}: {e}"[:300]
                log("   FAIL", msg)
                traceback.print_exc(file=sys.stderr)
                new_items += keep_old
                status[key] = {"bank": name, "status": "error", "count": len(keep_old), "message": msg,
                               "kept_old": len(keep_old), "last_success": prev.get("last_success"),
                               "seconds": round(time.time() - t0, 1), "via": "browser" if needs_browser else "http"}
    finally:
        BR.close()
    promos = dedup(new_items)
    promos.sort(key=lambda p: (p.get("valid_to") or "9999", p["bank"]))
    pfile.write_text(json.dumps({"updated_at": NOW.isoformat(timespec="seconds"), "count": len(promos), "promos": promos},
                                ensure_ascii=False, indent=1), encoding="utf-8")
    sfile.write_text(json.dumps({"updated_at": NOW.isoformat(timespec="seconds"),
                                 "runner": "github-actions" if os.environ.get("GITHUB_ACTIONS") else "local",
                                 "banks": status}, ensure_ascii=False, indent=1), encoding="utf-8")
    log("total", len(promos))
    ok = sum(1 for s in status.values() if s.get("status") == "ok")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
