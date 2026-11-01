"""
REDDIT SHORTS GENERATOR - Tam otomatik YouTube Shorts üretici

Yenilikler:
- Çeviri/uyarlama Gemini veya Claude ile: seslendirmeye uygun, hook'lu, yazım denetimli Türkçe
- Altyazı: edge-tts kelime zamanları (yoksa Whisper) + difflib hizalama -> kayma yok
- Türkçe büyük harf düzeltmesi (i -> İ, ı -> I)
- Arka plan her 5-10 saniyede bir, videonun FARKLI bir yerinden alınan klibe geçer
- Müzik miksajında ses düşmesi düzeltildi (amix normalize=0)

Kurulum:  pip install pillow google-genai edge-tts openai-whisper requests yt-dlp   (Claude için ayrıca: pip install anthropic)
API anahtarı: bu klasörde .env dosyası oluşturup içine şunu yaz:
    GEMINI_API_KEY=AIza...   (veya ANTHROPIC_API_KEY=sk-ant-...)
"""
import os
import sys
import json
import random
import re
import shutil
import asyncio
import argparse
import subprocess
from difflib import SequenceMatcher

import requests

# Windows konsolunda UTF-8
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ==================== AYARLAR ====================
WORK_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(WORK_DIR)


def load_dotenv(path=".env"):
    """Basit .env okuyucu (ek kütüphane gerekmez)."""
    p = os.path.join(WORK_DIR, path)
    if not os.path.exists(p):
        return
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_dotenv()

BG_MODE = "slime"                 # "slime" veya "minecraft"
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")
GEMINI_MODELS = [m for m in [os.environ.get("GEMINI_MODEL"), "gemini-3.5-flash", "gemini-2.5-flash"] if m]
PIXABAY_API_KEY = os.environ.get("PIXABAY_API_KEY", "")

# Arka plan klip uzunluğu aralığı (saniye) - her bu sürede bir sahne değişir
SEG_MIN = 5.0
SEG_MAX = 10.0

# Hedef metin uzunluğu (kelime). Edge-TTS Ahmet ~2.4 kelime/sn -> 130 kelime ~ 55 sn
WORDS_PER_SEC = 2.4          # Ahmet sesi yaklaşık bu hızda okuyor
TARGET_WORDS = 85
WORDS_TOLERANCE = (70, 100)
DEFAULT_TARGET_WORDS, DEFAULT_WORDS_TOLERANCE = TARGET_WORDS, WORDS_TOLERANCE

# Altyazı stili (referans görsel: beyaz, kalın yuvarlak geometrik font, ince siyah hat, küçük harf)
SUB_FONT = "Poppins"         # fonts/Poppins-Bold.ttf (yoksa otomatik indirilir)
SUB_FONT_FALLBACK = "Segoe UI"   # indirme başarısız olursa
SUB_SIZE = 84
SUB_BOLD = -1                # -1 = kalın (Poppins Bold), 0 = normal
SUB_OUTLINE = 3              # siyah dış hat kalınlığı
SUB_LOWERCASE = True         # True = küçük harf, False = BÜYÜK HARF
SUB_MAX_CHARS = 18           # bir altyazı grubunda en fazla harf (geniş font taşmasın)
FONT_DIR = os.path.join(WORK_DIR, "fonts")
FONT_FILE = os.path.join(FONT_DIR, "Poppins-Bold.ttf")
FONT_URLS = [
    "https://github.com/google/fonts/raw/main/ofl/poppins/Poppins-Bold.ttf",
    "https://cdn.jsdelivr.net/gh/google/fonts@main/ofl/poppins/Poppins-Bold.ttf",
    "https://cdn.jsdelivr.net/npm/@expo-google-fonts/poppins/700Bold/Poppins_700Bold.ttf",
]

VOICE = "tr-TR-AhmetNeural"
VOICE_RATE = "+5%"                # biraz tempolu anlatım

MINECRAFT_BG_FILE = os.path.join(WORK_DIR, "full_background.mp4")
SLIME_BG_FILE = os.path.join(WORK_DIR, "slime_background.mp4")
BG_MUSIC_FILE = os.path.join(WORK_DIR, "bg_music.mp3")
AUDIO_OUTPUT = os.path.join(WORK_DIR, "voice.mp3")
SUB_OUTPUT = os.path.join(WORK_DIR, "subtitles.ass")
SEG_DIR = os.path.join(WORK_DIR, "_segments")
LOCAL_FINAL_OUTPUT = os.path.join(WORK_DIR, "final_viral_shorts.mp4")
USED_FILE = os.path.join(WORK_DIR, "used_stories.txt")


def get_desktop_path():
    home = os.path.expanduser("~")
    onedrive_desktop = os.path.join(home, "OneDrive", "Desktop")
    if os.path.exists(onedrive_desktop):
        return onedrive_desktop
    return os.path.join(home, "Desktop")


DESKTOP_PATH = get_desktop_path()
# Çıktıların gideceği masaüstü klasörü (yoksa bot oluşturur)
OUTPUT_DIR = os.path.join(DESKTOP_PATH, "Reddit Shorts Videolar")


# ==================== TÜRKÇE METİN YARDIMCILARI ====================
def tr_lower(s: str) -> str:
    return s.replace("İ", "i").replace("I", "ı").lower()


def tr_upper(s: str) -> str:
    """Python'ın upper()'ı 'i'yi 'I' yapar (iş -> IŞ, yanlış). Doğrusu İŞ."""
    return s.replace("i", "İ").replace("ı", "I").upper()


def norm_word(w: str) -> str:
    return re.sub(r"[\W_]+", "", tr_lower(w), flags=re.UNICODE)


def sanitize_script(text: str) -> str:
    """TTS'i bozan karakterleri temizler."""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"[*_#>`~|]", "", text)
    text = re.sub(
        "[\U0001F300-\U0001FAFF☀-➿️‍]", "", text
    )
    text = text.replace("“", "").replace("”", "").replace('"', "")
    text = text.replace("’", "'").replace("…", "...")
    text = re.sub(r"\s+", " ", text).strip()
    return text


# ==================== MATERYALLER ====================
def ytdlp(args):
    subprocess.run([sys.executable, "-m", "yt_dlp", "--no-playlist"] + args, check=True)


def fetch_pixabay_slime_video(api_key: str, output_path: str) -> bool:
    try:
        print("[Pixabay] slime videosu aranıyor...")
        url = f"https://pixabay.com/api/videos/?key={api_key}&q=slime+satisfying&per_page=10"
        resp = requests.get(url, timeout=15)
        if resp.status_code == 200:
            hits = resp.json().get("hits", [])
            if hits:
                vd = random.choice(hits).get("videos", {})
                video_url = (vd.get("large") or vd.get("medium") or {}).get("url")
                if video_url:
                    r = requests.get(video_url, stream=True, timeout=30)
                    with open(output_path, "wb") as f:
                        for chunk in r.iter_content(chunk_size=8192):
                            f.write(chunk)
                    return True
    except Exception as e:
        print(f"[Pixabay] Hata: {e}")
    return False


def ensure_assets():
    if not os.path.exists(BG_MUSIC_FILE):
        print("[İndirme] bg_music.mp3 indiriliyor...")
        ytdlp(["-x", "--audio-format", "mp3", "-o", BG_MUSIC_FILE,
               "https://www.youtube.com/watch?v=GHEzUtkNuVo"])
    if BG_MODE == "slime":
        if not os.path.exists(SLIME_BG_FILE):
            ok = bool(PIXABAY_API_KEY) and fetch_pixabay_slime_video(PIXABAY_API_KEY, SLIME_BG_FILE)
            if not ok:
                ytdlp(["-f", "136/247/best[height<=720]", "-o", SLIME_BG_FILE,
                       "https://www.youtube.com/watch?v=lcPTDc9vHkE"])
    elif not os.path.exists(MINECRAFT_BG_FILE):
        ytdlp(["-f", "136/247/best[height<=720]", "-o", MINECRAFT_BG_FILE,
               "https://www.youtube.com/watch?v=u7kdVe8q5zs"])


def ensure_font():
    """Poppins Bold yoksa indirir; olmazsa yedek fonta geçer."""
    global SUB_FONT
    if SUB_FONT != "Poppins":
        return
    if os.path.exists(FONT_FILE) and os.path.getsize(FONT_FILE) > 50_000:
        return
    os.makedirs(FONT_DIR, exist_ok=True)
    for url in FONT_URLS:
        try:
            r = requests.get(url, timeout=30)
            if r.status_code == 200 and len(r.content) > 50_000 and r.content[:4] in (b"\x00\x01\x00\x00", b"true", b"OTTO"):
                with open(FONT_FILE, "wb") as f:
                    f.write(r.content)
                print("[Font] Poppins Bold indirildi.")
                return
        except Exception as e:
            print(f"[Font] {url[:50]}... indirilemedi: {e}")
    print(f"[Font] Poppins indirilemedi; yedek font kullanılıyor: {SUB_FONT_FALLBACK}. "
          "Elle çözüm: Poppins-Bold.ttf dosyasını fonts klasörüne koy.")
    SUB_FONT = SUB_FONT_FALLBACK


# ==================== KULLANILAN HİKAYE TAKİBİ ====================
def load_used():
    if not os.path.exists(USED_FILE):
        return set()
    with open(USED_FILE, "r", encoding="utf-8") as f:
        return {l.strip() for l in f if l.strip()}


def mark_used(key: str):
    with open(USED_FILE, "a", encoding="utf-8") as f:
        f.write(key + "\n")


# ==================== 1. HİKAYE KAYNAĞI ====================
TOPICS = [
    "düğünde ortaya çıkan büyük bir ihanet ve sürpriz intikam",
    "iş yerinde emeği çalınan çalışanın zekice hamlesi",
    "komşunun haksız şikayetinin kendisine dönmesi",
    "miras kavgasında ortaya çıkan gizli vasiyet",
    "yıllarca küçümsenen kişinin patronuyla yer değiştirmesi",
    "kayınvalidenin sınavında beklenmedik bir son",
    "kayıp sanılan bir eşyanın getirdiği büyük sır",
    "sınıf arkadaşının yıllar sonra özür dilemek zorunda kalması",
    "ev arkadaşının kirayı çalmaya çalışıp yakalanması",
    "aile yemeğinde patlayan gizli gerçek",
]


def clean_reddit_text(text: str) -> str:
    text = re.sub(r"http\S+", "", text)
    text = re.sub(r"\[.*?\]\(.*?\)", "", text)
    text = re.sub(r"Edit:.*", "", text, flags=re.IGNORECASE | re.S)
    text = re.sub(r"TL;?DR:?.*", "", text, flags=re.IGNORECASE | re.S)
    return re.sub(r"\s+", " ", text).strip()


def fetch_reddit_story(used: set):
    """Döner: (key, title, text) veya None."""
    subs = ["AITAH", "confession", "TrueOffMyChest", "relationship_advice"]
    random.shuffle(subs)
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                             "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
    for sub in subs:
        try:
            r = requests.get(f"https://www.reddit.com/r/{sub}/top.json?t=month&limit=25",
                             headers=headers, timeout=10)
            if r.status_code != 200:
                continue
            children = r.json().get("data", {}).get("children", [])
            random.shuffle(children)
            for ch in children:
                p = ch.get("data", {})
                key = "reddit:" + str(p.get("id"))
                body = p.get("selftext", "")
                if key in used or p.get("over_18") or not (250 <= len(body) <= 3000):
                    continue
                cleaned = clean_reddit_text(body)
                if len(cleaned) >= 200:
                    print(f"[Reddit] r/{sub}: {p.get('title')}")
                    return key, p.get("title", ""), cleaned
        except Exception as e:
            print(f"[Uyarı] r/{sub} çekilemedi: {e}")
    return None


# ==================== 2. CLAUDE İLE METİN ====================
def _system_prompt_text():
    return f"""Sen YouTube Shorts için Türkçe sesli hikaye yazan usta bir metin yazarısın.
Metin bir yapay zeka erkek sesiyle (TTS) okunacak ve aynı anda ekranda altyazı olarak akacak.

KURALLAR:
- Uzunluk: yaklaşık {TARGET_WORDS} kelime (en az {WORDS_TOLERANCE[0]}, en fazla {WORDS_TOLERANCE[1]}).
- İlk cümle HOOK olsun: ilk 3 saniyede merak uyandıran şaşırtıcı bir iddia veya sonuç. Selamlama, giriş yok.
- Birinci tekil şahıs ("ben"), konuşma dili, kısa cümleler (çoğu 6-12 kelime). Her cümle tek fikir.
- Gerilim kademeli artsın, sonda tatmin edici veya şok edici bir final olsun.
- Son cümle kısa bir soru olsun ki izleyici yorum yazsın (ör. "Sence haklı mıydım?").
- SESLENDİRMEYE UYGUN YAZ: rakam, sembol, kısaltma, emoji, parantez, tırnak, markdown, sahne notu KULLANMA.
  Sayıları yazıyla yaz ("2 saat" -> "iki saat", "%50" -> "yüzde elli", "TL" -> "lira").
- Doğal duraklamalar için noktalama kullan. Türkçe yazım ve noktalama kurallarına eksiksiz uy.
- Kaynak İngilizce ise çeviri yapma, Türkiye'ye UYARLA: Türkçe isimler, lira, Türkiye'deki günlük yaşam.
- BASLIK videonun başında Reddit gönderisi kartında gösterilecek ve ayrıca sesli okunacak: tek akıcı cümle, 60-110 karakter,
  olayı merak uyandıracak şekilde özetler (ör. "Eşim özgürleşme vaadiyle ailemizi terk etti; yıllar sonra pişmanlıkla kapıma dayandı.").
  Rakam, emoji, hashtag, tırnak, büyük harfle bağırma yok.
- METIN başlıktan hemen SONRA okunur: başlığı tekrar etme, doğrudan olaya gir. İlk cümlesi yine merak uyandırsın.
- Gerçek kişi, marka veya ünlü ismi kullanma. Grafik şiddet, cinsellik, nefret söylemi yok.

ÇIKTI FORMATI (aynen bu dört etiketle, başka hiçbir şey yazma, kod bloğu kullanma):
BASLIK: Reddit gönderi başlığı gibi tek cümle, 60-110 karakter
ACIKLAMA: YouTube açıklaması, tek satır, en fazla 200 karakter: önce merak uyandıran kısa bir cümle, sonra izleyiciye bir soru
HASHTAG: #shorts #hikaye #diğerleri
METIN:
seslendirilecek tam metin, tek paragraf"""


SYSTEM_PROMPT = _system_prompt_text()

PROOF_PROMPT = """Aşağıdaki metin bir TTS ile okunacak Türkçe Shorts anlatımıdır.
SADECE şunları düzelt: yazım hataları, noktalama, anlam bozukluğu, bozuk/yapay cümleler, rakam veya sembol kalmışsa yazıyla yaz.
Hikayeyi, tonu ve uzunluğu (±5 kelime) koru. Hook ilk cümle, soru son cümle kalsın.
Yalnızca düzeltilmiş düz metni döndür; açıklama, tırnak veya başlık ekleme.

METİN:
"""


class LLM:
    """Gemini (ücretsiz anahtar olur) veya Claude. Hangi anahtar .env'de varsa onu kullanır."""

    def __init__(self, provider, client):
        self.provider = provider
        self.client = client
        self.gemini_model = None  # çalışan ilk model hatırlanır

    def text(self, system, user, max_tokens=8192, temperature=0.9):
        if self.provider == "gemini":
            from google.genai import types
            base = dict(system_instruction=system, temperature=temperature, max_output_tokens=max_tokens)
            try:  # düşünme bütçesini kapat: çıktı limitini yemesin, daha hızlı olsun
                cfg = types.GenerateContentConfig(
                    thinking_config=types.ThinkingConfig(thinking_budget=0), **base)
            except Exception:
                cfg = types.GenerateContentConfig(**base)
            plain_cfg = types.GenerateContentConfig(**base)
            candidates = [self.gemini_model] if self.gemini_model else GEMINI_MODELS
            last_err = None
            for model in candidates:
                try:
                    try:
                        r = self.client.models.generate_content(model=model, contents=user, config=cfg)
                    except Exception as e1:
                        if "thinking" not in str(e1).lower():
                            raise
                        r = self.client.models.generate_content(model=model, contents=user, config=plain_cfg)
                    out = (r.text or "").strip()
                    if out:
                        self.gemini_model = model
                        return out
                except Exception as e:  # model adı yoksa/kota doluysa sıradakini dene
                    last_err = e
                    print(f"[Gemini] {model} başarısız: {str(e)[:150]}")
            raise RuntimeError(f"Gemini yanıt vermedi: {last_err}")
        msg = self.client.messages.create(
            model=CLAUDE_MODEL, max_tokens=max_tokens, temperature=temperature,
            system=system, messages=[{"role": "user", "content": user}],
        )
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()


def get_llm():
    gem = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    ant = os.environ.get("ANTHROPIC_API_KEY")
    pref = os.environ.get("LLM_PROVIDER", "").lower()
    order = ["gemini", "claude"] if pref != "claude" else ["claude", "gemini"]
    for prov in order:
        try:
            if prov == "gemini" and gem:
                from google import genai
                print("[Yapay zeka] Gemini kullanılıyor")
                return LLM("gemini", genai.Client(api_key=gem))
            if prov == "claude" and ant:
                import anthropic
                print("[Yapay zeka] Claude kullanılıyor")
                return LLM("claude", anthropic.Anthropic(api_key=ant))
        except ImportError as e:
            print(f"[Uyarı] Paket eksik ({e}). Gemini için: pip install google-genai | Claude için: pip install anthropic")
    return None


def parse_json_loose(raw: str) -> dict:
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    m = re.search(r"\{.*\}", raw, flags=re.S)
    return json.loads(m.group(0) if m else raw)


def parse_tagged(raw: str):
    """BASLIK/ACIKLAMA/HASHTAG/METIN etiketlerini ayıklar. Etiket yoksa ham metni script sayar."""
    raw = re.sub(r"```\w*", "", raw).strip()
    m_t = re.search(r"BA[ŞS]LIK\s*:\s*(.+)", raw, flags=re.I)
    m_d = re.search(r"A[ÇC]IKLAMA\s*:\s*(.+)", raw, flags=re.I)
    m_h = re.search(r"HASHTAG\w*\s*:\s*(.+)", raw, flags=re.I)
    m_s = re.search(r"MET[İI]N\s*:\s*(.+)", raw, flags=re.I | re.S)
    script = (m_s.group(1) if m_s else raw).strip()
    title = m_t.group(1).strip() if m_t else ""
    desc = m_d.group(1).strip() if m_d else ""
    tags = re.findall(r"#\w+", m_h.group(1)) if m_h else ["#shorts"]
    return title, script, (tags or ["#shorts"]), desc


def word_count(s: str) -> int:
    return len(s.split())


def write_script(client, source_title, source_text, topic):
    if source_text:
        user = (f"Kaynak başlık: {source_title}\n\nKaynak hikaye:\n{source_text[:3500]}\n\n"
                "Bu hikayeden esinlenerek yukarıdaki kurallara uygun özgün bir Shorts anlatımı yaz.")
    else:
        user = f"Konu: {topic}\n\nBu konuda kurgusal ama inandırıcı, özgün bir hikaye yaz."

    last = None
    for attempt in range(3):
        try:
            raw = client.text(SYSTEM_PROMPT, user)
            title, script, tags, desc = parse_tagged(raw)
            script = sanitize_script(script)
            n = word_count(script)
            if WORDS_TOLERANCE[0] <= n <= WORDS_TOLERANCE[1]:
                last = (title or source_title or "Hikaye", script, tags, sanitize_script(desc))
                break
            print(f"[Yapay zeka] Metin {n} kelime, aralık dışı; tekrar deneniyor... (yanıt başı: {raw[:120]!r})")
        except Exception as e:
            print(f"[Yapay zeka] Deneme {attempt + 1} başarısız: {e}")
    if not last:
        return None

    title, script, tags, desc = last
    # İkinci geçiş: yazım/noktalama denetimi (yalnızca seslendirilen metin)
    try:
        fixed = sanitize_script(client.text("Sen titiz bir Türkçe editörsün.",
                                            PROOF_PROMPT + script, temperature=0.2))
        if WORDS_TOLERANCE[0] <= word_count(fixed) <= WORDS_TOLERANCE[1]:
            script = fixed
    except Exception as e:
        print(f"[Yapay zeka] Denetim atlandı: {e}")
    return title, script, tags, desc


FALLBACK_STORIES = [
    ("Düğünümden İki Saat Önce Öğrendim",
     "Düğünüme iki saat kala eşimin telefonuna bir mesaj düştü. Gönderen en yakın arkadaşımdı. "
     "Mesajda mirasımı nasıl paylaşacaklarını yazmışlardı. Elim ayağım titredi ama ağlamadım. "
     "Gelinliğimi giyip nikah masasına oturdum. Memur soruyu sorunca mikrofonu aldım. "
     "Salondaki dev ekrana tüm mesajları yansıttım. Kimse nefes bile alamadı. "
     "İkisine de bir ömür mutluluklar dileyip alkışlarla salondan çıktım. "
     "O gün hayatımın en büyük özgürlüğünü yaşadım. Sence doğru mu yaptım?"),
]


def split_title(title: str, script: str):
    """Kart/seslendirme başlığı: LLM başlığı uygunsa onu, değilse metnin ilk cümlesini kullanır."""
    title = sanitize_script(title or "").strip()
    english = (not re.search(r"[çğıöşüÇĞİÖŞÜ]", title)
               and bool(re.search(r"\b(the|and|my|his|for|with|that|aita|wibta|told|about)\b", title.lower())))
    if not (15 <= len(title) <= 140) or english:
        m = re.match(r"(.+?[.!?])\s+(.*)", script, flags=re.S)
        if m:
            return m.group(1).strip(), m.group(2).strip()
    return title, script


def default_description() -> str:
    return ("Sizce haklı mıydı? Yorumlarda düşüncelerini yaz.\n\n"
            "Daha fazla gerçek hikaye için kanala abone olmayı unutma.")


def write_info_txt(path, title, desc, tags, script):
    """YouTube yüklemesi için başlık, açıklama, etiket ve metin dosyası."""
    desc = (desc or "").strip() or default_description()
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"BAŞLIK\n{title}\n\n")
        f.write(f"AÇIKLAMA\n{desc}\n\n")
        f.write(f"ETİKETLER\n{' '.join(tags)}\n\n")
        f.write(f"HİKAYE METNİ (seslendirilen)\n{script}\n")


def build_script(used: set, source: str):
    """Döner: (title, script, hashtags)."""
    client = get_llm()
    if client:
        item = fetch_reddit_story(used) if source == "reddit" else None
        if item:
            key, t, body = item
            res = write_script(client, t, body, None)
            if res:
                mark_used(key)
                return res
        topic = random.choice([t for t in TOPICS if "topic:" + t not in used] or TOPICS)
        res = write_script(client, None, None, topic)
        if res:
            mark_used("topic:" + topic)
            return res
    else:
        print("[Uyarı] Anahtar yok; yedek hikaye kullanılıyor (.env dosyasına GEMINI_API_KEY ekle).")
    t, s = random.choice(FALLBACK_STORIES)
    return t, s, ["#shorts", "#hikaye"], default_description()


# ==================== 3. SESLENDİRME + KELİME ZAMANLARI ====================
async def generate_voice(text: str, output_path: str):
    """Sesi üretir. edge-tts kelime sınırlarını da yakalayıp döndürür (varsa)."""
    import edge_tts
    try:
        comm = edge_tts.Communicate(text, VOICE, rate=VOICE_RATE, boundary="WordBoundary")
    except TypeError:  # eski sürüm: WordBoundary zaten varsayılan
        comm = edge_tts.Communicate(text, VOICE, rate=VOICE_RATE)
    words = []
    with open(output_path, "wb") as f:
        async for ch in comm.stream():
            if ch["type"] == "audio":
                f.write(ch["data"])
            elif ch["type"] == "WordBoundary":
                s = ch["offset"] / 1e7
                words.append({"word": ch["text"], "start": s, "end": s + ch["duration"] / 1e7})
    return words


def whisper_words(audio_path: str, reference_text: str):
    import whisper
    print("[Whisper] Zamanlama çıkarılıyor...")
    model = whisper.load_model("small")
    res = model.transcribe(audio_path, language="tr", word_timestamps=True,
                           initial_prompt=reference_text[:400])
    out = []
    for seg in res.get("segments", []):
        for w in seg.get("words", []):
            out.append({"word": w["word"].strip(), "start": w["start"], "end": w["end"]})
    return out


# ==================== 4. HİZALAMA + ALTYAZI ====================
def align_words(ref_tokens, timed, audio_dur):
    """
    Referans (doğru yazılmış) kelimelere zaman verir.
    difflib ile eşler; eşleşmeyen kelimeleri komşu zamanlar arasında harf sayısına göre dağıtır.
    Böylece Whisper/TTS kelime atlasa, bölse veya yanlış duysa bile kayma olmaz.
    """
    a = [norm_word(w) for w in ref_tokens]
    b = [norm_word(t["word"]) for t in timed]
    times = [None] * len(ref_tokens)
    sm = SequenceMatcher(None, a, b, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal" or (tag == "replace" and (i2 - i1) == (j2 - j1)):
            for k in range(i2 - i1):
                times[i1 + k] = (timed[j1 + k]["start"], timed[j1 + k]["end"])

    # Boşlukları doldur
    n = len(times)
    i = 0
    while i < n:
        if times[i] is not None:
            i += 1
            continue
        j = i
        while j < n and times[j] is None:
            j += 1
        t0 = times[i - 1][1] if i > 0 else 0.0
        t1 = times[j][0] if j < n else audio_dur
        t1 = max(t1, t0)
        weights = [max(len(a[k]), 1) for k in range(i, j)]
        total = sum(weights)
        cur = t0
        for k, wgt in zip(range(i, j), weights):
            dur = (t1 - t0) * wgt / total
            times[k] = (cur, cur + dur)
            cur += dur
        i = j
    # Monoton olmayanları düzelt
    prev_end = 0.0
    fixed = []
    for s, e in times:
        s = max(s, prev_end)
        e = max(e, s + 0.05)
        fixed.append((s, e))
        prev_end = e
    return fixed


def display_form(token: str) -> str:
    t = token.strip()
    t = re.sub(r"^[\"'(\[«]+|[\"')\]»]+$", "", t)
    suffix = "?" if t.endswith("?") else "!" if t.endswith("!") else ""
    t = re.sub(r"[.,;:…?!]+$", "", t)
    return (tr_lower(t) if SUB_LOWERCASE else tr_upper(t)) + suffix


def make_chunks(tokens, times, max_words=3, gap=0.45):
    """Altyazı gruplarını cümle/virgül sınırlarına ve kelime boşluklarına göre keser."""
    chunks, cur = [], []
    for idx, tok in enumerate(tokens):
        cur.append(idx)
        end_sentence = bool(re.search(r"[.?!…]$", tok))
        end_clause = bool(re.search(r"[,;:]$", tok)) and len(cur) >= 2
        big_gap = idx + 1 < len(tokens) and (times[idx + 1][0] - times[idx][1]) > gap
        chars = sum(len(display_form(tokens[k])) + 1 for k in cur)
        nxt_len = len(display_form(tokens[idx + 1])) + 1 if idx + 1 < len(tokens) else 0
        too_long = chars + nxt_len > SUB_MAX_CHARS
        if len(cur) >= max_words or end_sentence or end_clause or big_gap or too_long:
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    return chunks


def fmt_time(sec: float) -> str:
    sec = max(sec, 0.0)
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    cs = int(round((sec - int(sec)) * 100))
    if cs == 100:
        s += 1
        cs = 0
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},{size},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,{bold},0,0,0,100,100,0,0,1,{outline},1,5,80,80,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def write_ass(tokens, times, audio_dur, ass_path):
    chunks = make_chunks(tokens, times)
    lines = []
    for ci, idxs in enumerate(chunks):
        start = times[idxs[0]][0]
        end = times[idxs[-1]][1]
        if ci + 1 < len(chunks):
            nxt = times[chunks[ci + 1][0]][0]
            if nxt - end < 0.6:      # küçük boşluklarda altyazı ekranda kalsın (titreme olmasın)
                end = nxt
        else:
            end = min(max(end, start + 0.3) + 0.3, audio_dur + 0.3)
        text = " ".join(display_form(tokens[k]) for k in idxs)
        pop = r"{\an5\fscx118\fscy118\t(0,90,\fscx100\fscy100)}"
        lines.append(f"Dialogue: 0,{fmt_time(start)},{fmt_time(end)},Default,,0,0,0,,{pop}{text}")
    with open(ass_path, "w", encoding="utf-8") as f:
        header = (ASS_HEADER.replace("{font}", SUB_FONT).replace("{size}", str(SUB_SIZE))
                  .replace("{bold}", str(SUB_BOLD)).replace("{outline}", str(SUB_OUTLINE)))
        f.write(header + "\n".join(lines) + "\n")


def build_subtitles(script, audio_path, ass_path, boundary_words, audio_dur, skip=0):
    """skip: baştaki kaç kelime altyazıda GÖSTERİLMESİN (kartta okunan başlık). Başlığın bittiği zamanı döndürür."""
    tokens = script.split()
    timed = boundary_words
    if len(timed) < max(5, len(tokens) // 3):
        timed = whisper_words(audio_path, script)
    times = align_words(tokens, timed, audio_dur)
    write_ass(tokens[skip:], times[skip:], audio_dur, ass_path)
    print(f"[Altyazı] {len(tokens) - skip} kelime hizalandı -> {ass_path}")
    return times[skip - 1][1] if skip else 0.0


# ==================== REDDIT POST KARTI ====================
CARD_NAME = "Reddit Deliği"                      # kartta görünen kanal adı
CARD_AVATAR = os.path.join(WORK_DIR, "avatar.png")
CARD_PNG = os.path.join(WORK_DIR, "reddit_card.png")
CARD_WIDTH = 930
CARD_EXTRA_SEC = 0.35                            # başlık bittikten sonra kart bu kadar daha kalır


def _card_font(size: int):
    from PIL import ImageFont
    cands = [r"C:\Windows\Fonts\segoeuib.ttf", r"C:\Windows\Fonts\arialbd.ttf", FONT_FILE,
             "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
    for p in cands:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _wrap(text, font, max_w):
    lines, cur = [], ""
    for w in text.split():
        test = (cur + " " + w).strip()
        if font.getlength(test) <= max_w or not cur:
            cur = test
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _star(cx, cy, r_out, r_in, n):
    import math
    pts = []
    for i in range(n * 2):
        r = r_out if i % 2 == 0 else r_in
        a = math.pi * i / n - math.pi / 2
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def _award(d, cx, cy, r, kind):
    """Renkli Reddit ödül madalyası (vektör çizim, emoji fontuna bağımlı değil)."""
    palette = [("#FFB000", "#FFE27A"), ("#46D160", "#C2F5CC"), ("#FF585B", "#FFC9CA"),
               ("#0079D3", "#A9D8FF"), ("#FF8717", "#FFD6AD"), ("#7193FF", "#D3DDFF"),
               ("#EA0027", "#FFB3BF"), ("#B77DFF", "#E8D5FF"), ("#00A6A5", "#B0F0EF"),
               ("#FFD635", "#FFF3B0")]
    outer, inner = palette[kind % len(palette)]
    shape = kind % 5
    if shape == 0:      # madalya
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=outer)
        d.ellipse([cx - r * .6, cy - r * .6, cx + r * .6, cy + r * .6], fill=inner)
    elif shape == 1:    # yıldız
        d.polygon(_star(cx, cy, r * 1.1, r * .55, 5), fill=outer)
        d.ellipse([cx - r * .3, cy - r * .3, cx + r * .3, cy + r * .3], fill=inner)
    elif shape == 2:    # elmas
        d.polygon([(cx, cy - r * 1.1), (cx + r, cy), (cx, cy + r * 1.1), (cx - r, cy)], fill=outer)
        d.polygon([(cx, cy - r * .55), (cx + r * .5, cy), (cx, cy + r * .55), (cx - r * .5, cy)], fill=inner)
    elif shape == 3:    # kalkan
        d.polygon([(cx - r, cy - r), (cx + r, cy - r), (cx + r, cy + r * .2), (cx, cy + r * 1.1), (cx - r, cy + r * .2)], fill=outer)
        d.ellipse([cx - r * .4, cy - r * .55, cx + r * .4, cy + r * .25], fill=inner)
    else:               # çiçek/yıldız
        d.polygon(_star(cx, cy, r * 1.1, r * .75, 8), fill=outer)
        d.ellipse([cx - r * .4, cy - r * .4, cx + r * .4, cy + r * .4], fill=inner)


def generate_reddit_card(title: str, output_png: str = CARD_PNG, name: str = CARD_NAME) -> str:
    """Şeffaf zeminli, gerçek Reddit gönderisi gibi beyaz kart PNG'si üretir (Pillow)."""
    from PIL import Image, ImageDraw
    S = 3                                   # 3x çiz, küçült -> pürüzsüz kenarlar
    W, pad, r_card = CARD_WIDTH, 40, 30
    av = 108
    f_name, f_title, f_foot = _card_font(40 * S), _card_font(46 * S), _card_font(36 * S)

    lines = _wrap(title, f_title, (W - 2 * pad) * S)
    line_h = int(46 * 1.34)
    top, gap1, gap2, foot_h, bottom = 38, 34, 30, 44, 36
    H = top + av + gap1 + len(lines) * line_h + gap2 + foot_h + bottom

    img = Image.new("RGBA", (W * S, H * S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, W * S - 1, H * S - 1], radius=r_card * S, fill="#FFFFFF")

    # Avatar (daire)
    ax, ay = pad, top
    try:
        a = Image.open(CARD_AVATAR).convert("RGB")
        m = int(a.width * 0.03)
        a = a.crop((m, m, a.width - m, a.height - m)).resize((av * S, av * S), Image.LANCZOS)
    except Exception:
        a = Image.new("RGB", (av * S, av * S), "#FF4500")
    mask = Image.new("L", (av * S, av * S), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, av * S - 1, av * S - 1], fill=255)
    img.paste(a, (ax * S, ay * S), mask)

    # İsim + ödüller
    tx = ax + av + 22
    d.text((tx * S, (ay + 8) * S), name, font=f_name, fill="#1C1C1C")
    ar = 15
    for i in range(10):
        _award(d, (tx + ar + i * (ar * 2 + 8)) * S, (ay + 82) * S, ar * S, i)

    # Başlık
    y = ay + av + gap1
    for ln in lines:
        d.text((pad * S, y * S), ln, font=f_title, fill="#1C1C1C")
        y += line_h

    # Alt satır: upvote oku + yorum balonu
    gray = "#7C7C7C"
    fy = y + gap2 - 6
    cy = fy + foot_h / 2
    lw = 4 * S
    a_ = 38
    cx = pad + a_ / 2
    arrow = [(cx, cy - a_ / 2), (cx + a_ / 2, cy + 2), (cx + a_ / 6, cy + 2), (cx + a_ / 6, cy + a_ / 2),
             (cx - a_ / 6, cy + a_ / 2), (cx - a_ / 6, cy + 2), (cx - a_ / 2, cy + 2), (cx, cy - a_ / 2)]
    d.line([(px * S, py * S) for px, py in arrow], fill=gray, width=lw, joint="curve")
    d.text(((pad + a_ + 14) * S, (cy - 24) * S), "99+", font=f_foot, fill=gray)

    bx = pad + 210
    d.rounded_rectangle([bx * S, (cy - 17) * S, (bx + 40) * S, (cy + 11) * S], radius=10 * S, outline=gray, width=lw)
    d.line([((bx + 9) * S, (cy + 11) * S), ((bx + 9) * S, (cy + 22) * S), ((bx + 21) * S, (cy + 11) * S)],
           fill=gray, width=lw, joint="curve")
    d.text(((bx + 40 + 14) * S, (cy - 24) * S), "99+", font=f_foot, fill=gray)

    img = img.resize((W, H), Image.LANCZOS)
    img.save(output_png)
    return output_png


# ==================== 5. RENDER ====================
def media_duration(path: str, default=600.0) -> float:
    try:
        out = subprocess.check_output([
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", path]).decode().strip()
        return float(out)
    except Exception:
        return default


def plan_segments(total: float, bg_dur: float):
    """Toplam süreyi 5-10 sn'lik parçalara böler; her parça için arka plan videosunda FARKLI bir başlangıç seçer."""
    durs, t = [], 0.0
    while t < total - 0.05:
        d = round(random.uniform(SEG_MIN, SEG_MAX), 1)
        if total - t - d < 2.5:       # çok kısa artık parça bırakma
            d = round(total - t, 2)
        durs.append(d)
        t += d

    lo, hi_margin = min(10.0, bg_dur * 0.05), SEG_MAX + 2
    chosen = []
    plan = []
    for d in durs:
        start = None
        for _ in range(60):
            cand = random.uniform(lo, max(lo + 1, bg_dur - d - 2))
            if all(cand + d + 3 < s or cand > s + dd + 3 for s, dd in chosen):
                start = cand
                break
        if start is None:   # video kısaysa çakışmaya izin ver
            start = random.uniform(lo, max(lo + 1, bg_dur - d - 2))
        chosen.append((start, d))
        plan.append((round(start, 2), d))
    return plan


def render_segments(bg_video: str, total: float):
    if os.path.isdir(SEG_DIR):
        shutil.rmtree(SEG_DIR, ignore_errors=True)
    os.makedirs(SEG_DIR, exist_ok=True)
    bg_dur = media_duration(bg_video)
    plan = plan_segments(total, bg_dur)
    vf = ("scale=1080:1920:force_original_aspect_ratio=increase,"
          "crop=1080:1920,setsar=1,fps=30,format=yuv420p")
    list_lines = []
    for i, (start, d) in enumerate(plan):
        name = f"seg_{i:03d}.mp4"
        out = os.path.join("_segments", name)
        subprocess.run([
            "ffmpeg", "-y", "-loglevel", "error", "-ss", str(start), "-t", str(d), "-i", bg_video,
            "-an", "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", out
        ], check=True)
        list_lines.append(f"file '{name}'")  # concat, yolu list.txt'nin klasörüne göre çözer
        print(f"  [Sahne {i + 1}/{len(plan)}] {d:.1f}s  (kaynak {start:.0f}. sn)")
    list_path = os.path.join(SEG_DIR, "list.txt")
    with open(list_path, "w", encoding="utf-8") as f:
        f.write("\n".join(list_lines) + "\n")
    return list_path


def render_shorts(bg_video, bg_music, voice_audio, ass_path, output_path, card_png=None, card_end=0.0):
    total = media_duration(voice_audio, 55.0) + 0.6
    print(f"[FFmpeg] Süre {total:.1f}s; 5-10 sn'lik arka plan sahneleri hazırlanıyor...")
    list_path = render_segments(bg_video, total)

    sub = os.path.basename(ass_path)  # cwd = WORK_DIR, Windows yol sorunlarını önler
    fontsdir_opt = ":fontsdir='fonts'" if os.path.isdir(FONT_DIR) else ""
    use_card = bool(card_png) and os.path.exists(card_png) and card_end > 0.5
    if use_card:
        fade = 0.25
        video_chain = (
            f"[0:v]subtitles=filename='{sub}'{fontsdir_opt}[vs];"
            f"[3:v]format=rgba,fade=t=in:st=0:d={fade}:alpha=1,"
            f"fade=t=out:st={max(card_end - fade, 0.1):.2f}:d={fade}:alpha=1[card];"
            f"[vs][card]overlay=(W-w)/2:(H-h)/2:enable='lte(t,{card_end:.2f})'[v];"
        )
    else:
        video_chain = f"[0:v]subtitles=filename='{sub}'{fontsdir_opt}[v];"
    fc = (
        video_chain +
        "[1:a]volume=1.0[av];[2:a]volume=0.10[am];"
        "[av][am]amix=inputs=2:duration=first:dropout_transition=2:normalize=0,"
        "afade=t=out:st=" + f"{max(total - 1.0, 0):.2f}" + ":d=1[a]"
    )
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", list_path.replace(os.sep, "/"),
        "-i", voice_audio,
        "-stream_loop", "-1", "-i", bg_music,
    ] + (["-framerate", "30", "-loop", "1", "-i", card_png.replace(os.sep, "/")] if use_card else []) + [
        "-filter_complex", fc, "-map", "[v]", "-map", "[a]",
        "-t", f"{total:.2f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", output_path
    ]
    subprocess.run(cmd, check=True)
    shutil.rmtree(SEG_DIR, ignore_errors=True)
    print(f"[FFmpeg] Render tamamlandı: {output_path}")


# ==================== TERMİNAL GÖRÜNÜMÜ ====================
def _enable_ansi():
    """Windows konsolunda renk kodlarını açar."""
    if sys.platform == "win32":
        os.system("")


_enable_ansi()
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


class C:
    R = "\033[0m"; B = "\033[1m"; DIM = "\033[2m"
    RED = "\033[91m"; GRN = "\033[92m"; YEL = "\033[93m"
    BLU = "\033[94m"; MAG = "\033[95m"; CYN = "\033[96m"; GRY = "\033[90m"


def banner(count, source, bg):
    w = 60
    print()
    print(f"{C.MAG}╔{'═' * w}╗{C.R}")
    print(f"{C.MAG}║{C.R}{C.B}{'REDDIT SHORTS GENERATOR'.center(w)}{C.R}{C.MAG}║{C.R}")
    print(f"{C.MAG}╚{'═' * w}╝{C.R}")
    print(f"  {C.GRY}Video sayısı{C.R} {C.B}{count}{C.R}    {C.GRY}Kaynak{C.R} {source}    {C.GRY}Arka plan{C.R} {bg}")


def step(tag, msg):
    print(f"{C.CYN}{C.B}[{tag}]{C.R} {msg}")


def ok(msg):
    print(f"{C.GRN}✔{C.R} {msg}")


def warn(msg):
    print(f"{C.YEL}⚠{C.R} {msg}")


def err(msg):
    print(f"{C.RED}✖{C.R} {msg}")


def video_header(n, total):
    print(f"\n{C.MAG}━━━━━━━━━━━━━━━━━━━━━━ VİDEO {n}/{total} ━━━━━━━━━━━━━━━━━━━━━━{C.R}")


def progress_bar(done, total, width=30):
    filled = int(width * done / max(total, 1))
    return f"{C.GRN}{'█' * filled}{C.GRY}{'░' * (width - filled)}{C.R} {done}/{total}"


def summary(done, total):
    print()
    print(f"{C.GRN}{C.B}✔ {done}/{total} video tamamlandı{C.R}")
    if done < total:
        print(f"{C.YEL}⚠ {total - done} video başarısız, yukarıdaki hatalara bak{C.R}")
    print(f"{C.GRY}Klasör: {OUTPUT_DIR}{C.R}")


# ==================== ANA AKIŞ ====================
async def make_one(source: str):
    used = load_used()
    title, script, tags, desc = build_script(used, source)
    title, script = split_title(title, script)
    # Başlık önce seslendirilir ve kartta gösterilir; hikaye hemen ardından başlar
    spoken_title = title if re.search(r"[.!?…]$", title) else title + "."
    spoken = f"{spoken_title} {script}"
    n_title = len(spoken_title.split())
    print(f"\n{C.B}{title}{C.R}")
    print(f"{C.GRY}{word_count(script)} kelime{C.R}")
    print(f"{C.GRY}{script}{C.R}\n")

    step("1/3", "Seslendiriliyor (edge-tts)")
    boundary = await generate_voice(spoken, AUDIO_OUTPUT)
    audio_dur = media_duration(AUDIO_OUTPUT, 55.0)

    step("2/3", "Altyazı zamanlanıyor")
    title_end = build_subtitles(spoken, AUDIO_OUTPUT, SUB_OUTPUT, boundary, audio_dur, skip=n_title)

    step("Kart", "Reddit kartı çiziliyor")
    try:
        generate_reddit_card(spoken_title, CARD_PNG)
        card_end = title_end + CARD_EXTRA_SEC
    except Exception as e:
        warn(f"Kart çizilemedi, kartsız devam: {e}")
        card_end = 0.0

    active_bg = SLIME_BG_FILE if BG_MODE == "slime" else MINECRAFT_BG_FILE
    step("3/3", f"Render · {BG_MODE}")
    render_shorts(active_bg, BG_MUSIC_FILE, AUDIO_OUTPUT, SUB_OUTPUT, LOCAL_FINAL_OUTPUT,
                  card_png=CARD_PNG, card_end=card_end)

    try:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        idx = 1
        while os.path.exists(os.path.join(OUTPUT_DIR, f"final_viral_shorts_{idx}.mp4")):
            idx += 1
        dst = os.path.join(OUTPUT_DIR, f"final_viral_shorts_{idx}.mp4")
        shutil.copy2(LOCAL_FINAL_OUTPUT, dst)
        # YouTube yüklemesi için başlık/açıklama dosyası
        write_info_txt(dst.replace(".mp4", ".txt"), title, desc, tags, script)
        ok(f"Video hazır: {dst}")
    except Exception as e:
        print(f"\nMasaüstüne kopyalanamadı: {e}. Yerel dosya: {LOCAL_FINAL_OUTPUT}")


async def run_batch(count, source, bg, progress=None, seconds=None):
    """Verilen sayıda videoyu sırayla üretir. progress(i) her videodan sonra çağrılır."""
    global BG_MODE, TARGET_WORDS, WORDS_TOLERANCE, SYSTEM_PROMPT
    BG_MODE = bg
    if seconds:
        TARGET_WORDS = round(seconds * WORDS_PER_SEC)
        WORDS_TOLERANCE = (round(TARGET_WORDS * 0.82), round(TARGET_WORDS * 1.18))
    else:
        TARGET_WORDS, WORDS_TOLERANCE = DEFAULT_TARGET_WORDS, DEFAULT_WORDS_TOLERANCE
    SYSTEM_PROMPT = _system_prompt_text()
    banner(count, source, bg)
    if seconds:
        print(f"  {C.GRY}Hedef süre{C.R} ~{seconds} sn ({TARGET_WORDS} kelime)")
    ensure_assets()
    ensure_font()
    done = 0
    for n in range(1, count + 1):
        video_header(n, count)
        try:
            await make_one(source)
            done += 1
        except Exception as e:
            err(f"Video {n} başarısız: {e}")
        if progress:
            progress(n)
        print(f"  {progress_bar(n, count)}")
    summary(done, count)


def build_parser():
    ap = argparse.ArgumentParser(description="Reddit Shorts Generator")
    ap.add_argument("--count", type=int, default=1, help="Kaç video üretilsin (1-50)")
    ap.add_argument("--source", choices=["reddit", "ai"], default="reddit",
                    help="reddit: Reddit'ten uyarla, ai: konudan özgün hikaye")
    ap.add_argument("--bg", choices=["slime", "minecraft"], default="slime")
    ap.add_argument("--seconds", type=int, default=None, help="Hedef video süresi (20-90 sn)")
    ap.add_argument("--gui", action="store_true", help="Pencereli arayüzü aç")
    return ap


class QueueWriter:
    """print çıktısını GUI'deki kutuya taşır."""
    def __init__(self, q):
        self.q = q

    def write(self, text):
        if text:
            self.q.put(("log", text))
        return len(text)

    def flush(self):
        pass


def launch_gui():
    import queue
    import threading
    import tkinter as tk
    from tkinter import ttk

    q = queue.Queue()
    BGC, FG, CARD, ACC = "#0f0f13", "#e8e8ee", "#1a1a22", "#ff4500"

    root = tk.Tk()
    root.title("Reddit Shorts Generator")
    root.geometry("820x640")
    root.minsize(660, 500)
    root.configure(bg=BGC)

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:
        pass
    style.configure("TFrame", background=BGC)
    style.configure("Card.TFrame", background=CARD)
    style.configure("TLabel", background=BGC, foreground=FG, font=("Segoe UI", 10))
    style.configure("Head.TLabel", background=BGC, foreground=ACC, font=("Segoe UI", 18, "bold"))
    style.configure("Sub.TLabel", background=BGC, foreground="#8a8a96", font=("Segoe UI", 9))
    style.configure("Card.TLabel", background=CARD, foreground=FG, font=("Segoe UI", 10))
    style.configure("TCombobox", fieldbackground=CARD, foreground=FG, background=CARD)
    style.configure("TSpinbox", fieldbackground=CARD, foreground=FG, background=CARD, arrowcolor=FG)
    style.configure("Go.TButton", background=ACC, foreground="white",
                    font=("Segoe UI", 11, "bold"), padding=(18, 8))
    style.map("Go.TButton", background=[("disabled", "#5a2a18"), ("active", "#ff6a33")])
    style.configure("Ghost.TButton", background=CARD, foreground=FG, padding=(12, 8))
    style.configure("Horizontal.TProgressbar", background=ACC, troughcolor=CARD,
                    bordercolor=CARD, lightcolor=ACC, darkcolor=ACC)

    top = ttk.Frame(root, padding=(24, 20, 24, 8))
    top.pack(fill="x")
    ttk.Label(top, text="Reddit Shorts Generator", style="Head.TLabel").pack(anchor="w")
    ttk.Label(top, text="Kaç video üretilsin? Ayarları seç, başlat.",
              style="Sub.TLabel").pack(anchor="w", pady=(2, 14))

    card = ttk.Frame(root, style="Card.TFrame", padding=18)
    card.pack(fill="x", padx=24)
    count_s = tk.StringVar(value="1")
    src_var = tk.StringVar(value="reddit")
    bg_var = tk.StringVar(value="slime")
    sec_s = tk.StringVar(value="40")

    ttk.Label(card, text="Video sayısı (1-50)", style="Card.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 18))
    ttk.Label(card, text="Kaynak", style="Card.TLabel").grid(row=0, column=1, sticky="w", padx=(0, 18))
    ttk.Label(card, text="Arka plan", style="Card.TLabel").grid(row=0, column=2, sticky="w", padx=(0, 18))
    ttk.Label(card, text="Süre (sn, 20-90)", style="Card.TLabel").grid(row=0, column=3, sticky="w")
    ttk.Spinbox(card, from_=20, to=90, textvariable=sec_s, width=8, font=("Segoe UI", 12)).grid(
        row=1, column=3, sticky="w", pady=(6, 0))
    ttk.Spinbox(card, from_=1, to=50, textvariable=count_s, width=8, font=("Segoe UI", 12)).grid(
        row=1, column=0, sticky="w", padx=(0, 18), pady=(6, 0))
    ttk.Combobox(card, textvariable=src_var, values=["reddit", "ai"], state="readonly", width=12,
                 font=("Segoe UI", 11)).grid(row=1, column=1, sticky="w", padx=(0, 18), pady=(6, 0))
    ttk.Combobox(card, textvariable=bg_var, values=["slime", "minecraft"], state="readonly", width=12,
                 font=("Segoe UI", 11)).grid(row=1, column=2, sticky="w", pady=(6, 0))

    btns = ttk.Frame(card, style="Card.TFrame")
    btns.grid(row=2, column=0, columnspan=4, sticky="w", pady=(18, 0))
    running = {"v": False}

    def open_folder():
        p = OUTPUT_DIR if os.path.isdir(OUTPUT_DIR) else DESKTOP_PATH
        try:
            os.startfile(p)
        except Exception as e:
            txt.insert("end", f"Klasör açılamadı: {e}\n")

    start_btn = ttk.Button(btns, text="▶  Başlat", style="Go.TButton", command=lambda: start())
    start_btn.pack(side="left")
    ttk.Button(btns, text="Klasörü aç", style="Ghost.TButton", command=open_folder).pack(side="left", padx=10)

    mid = ttk.Frame(root, padding=(24, 16, 24, 6))
    mid.pack(fill="x")
    status = tk.StringVar(value="Hazır")
    ttk.Label(mid, textvariable=status).pack(anchor="w")
    pb = ttk.Progressbar(mid, maximum=1, value=0)
    pb.pack(fill="x", pady=6)

    logf = tk.Frame(root, bg=BGC)
    logf.pack(fill="both", expand=True, padx=24, pady=(0, 20))
    txt = tk.Text(logf, bg="#0a0a0d", fg="#d6d6de", insertbackground=FG, font=("Consolas", 10),
                  relief="flat", padx=12, pady=10, wrap="word", borderwidth=0)
    sb = ttk.Scrollbar(logf, command=txt.yview)
    txt.configure(yscrollcommand=sb.set)
    txt.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")

    def worker(n, src, bg, secs):
        old = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = QueueWriter(q)
        try:
            asyncio.run(run_batch(n, src, bg, progress=lambda i: q.put(("prog", (i, n))), seconds=secs))
            q.put(("done", "ok"))
        except Exception as e:
            print(f"\n✖ Hata: {e}")
            q.put(("done", "err"))
        finally:
            sys.stdout, sys.stderr = old

    def start():
        if running["v"]:
            return
        try:
            n = int(count_s.get())
        except ValueError:
            n = 1
        n = max(1, min(50, n))
        running["v"] = True
        start_btn.state(["disabled"])
        txt.delete("1.0", "end")
        pb.configure(maximum=n, value=0)
        status.set(f"Başlıyor… 0/{n}")
        try:
            secs = max(20, min(90, int(sec_s.get())))
        except ValueError:
            secs = 40
        threading.Thread(target=worker, args=(n, src_var.get(), bg_var.get(), secs), daemon=True).start()

    def poll():
        try:
            while True:
                kind, val = q.get_nowait()
                if kind == "log":
                    txt.insert("end", ANSI_RE.sub("", val))
                    txt.see("end")
                elif kind == "prog":
                    i, total = val
                    pb.configure(value=i)
                    status.set(f"{i}/{total} video işlendi")
                elif kind == "done":
                    running["v"] = False
                    start_btn.state(["!disabled"])
                    status.set("Tamamlandı ✔" if val == "ok" else "Hata oluştu ✖")
        except queue.Empty:
            pass
        root.after(80, poll)

    poll()
    root.mainloop()


if __name__ == "__main__":
    if len(sys.argv) == 1:
        launch_gui()  # argümansız çalıştırınca pencere açılır
    else:
        args = build_parser().parse_args()
        if args.gui:
            launch_gui()
        else:
            secs = max(20, min(90, args.seconds)) if args.seconds else None
            asyncio.run(run_batch(max(1, min(50, args.count)), args.source, args.bg, seconds=secs))
