# Reddit Shorts Generator

Reddit'ten (veya yapay zekadan) hikaye alıp, Türkçe seslendirilmiş, altyazılı, dikey YouTube Shorts videosu üreten Python aracı.

## Özellikler
- Hikayeyi Gemini veya Claude ile Türkçe Shorts formatına uyarlar
- Edge-TTS ile doğal Türkçe seslendirme
- Kelime zamanlarına göre senkron, tam ortada, küçük harf altyazı
- Başta Reddit tarzı başlık kartı
- 5-10 saniyede bir değişen arka plan sahneleri
- Pencereli arayüz (toplu üretim) ve renkli terminal çıktısı
- Her video için YouTube başlık, açıklama ve etiket dosyası

## Kurulum
1. Python 3.11+ ve FFmpeg kurulu olmalı (PATH'te çalışmalı).
2. Paketleri kur:
   ```
   pip install -r requirements.txt
   ```
3. `.env.example` dosyasını kopyala, adını `.env` yap ve kendi anahtarını yaz.
4. İlk çalıştırmada arka plan videoları ve müzik otomatik indirilir.

## Kullanım
- Pencere: `python auto_shorts.py`
- Terminal: `python auto_shorts.py --count 5 --source reddit --bg slime --seconds 40`

Çıktılar masaüstündeki `Reddit Shorts Videolar` klasörüne kaydedilir.

## Notlar
- Reddit hikayeleri telif ve YouTube "tekrarlanan içerik" politikalarına dikkat ederek kullanılmalı. Her videoya kendi özgün dokunuşunu eklemen önerilir.
- Anahtarlarını kimseyle paylaşma. Bir anahtar yanlışlıkla açığa çıktıysa hemen iptal edip yenisini oluştur.
