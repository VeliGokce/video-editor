# Media Editor

MP4 videolar için özelleştirilmiş, Türkçe ve İngilizce Windows video editörü.

- Kırpma ve bölüm çıkarma
- Başa, sona veya kaynak zamanına video ekleme
- Görsel bindirme
- Yatay/dikey encoding profilleri
- CPU ve GPU hızlandırma
- Kalıcı hızlı ayarlar
- İşlem öncesi doğrulama ve güvenli iptal
- FFprobe ile kesin ana-kare doğrulaması
- Güvenli koşullarda kayıpsız kırpma/birleştirme, diğer her durumda full encoding

## GitHub üzerinden EXE oluşturma

Her `main` güncellemesinde GitHub Actions iki bağımsız EXE üretir:

- `MediaEditor-Win10-Win11.exe`
- `MediaEditor-Win7.exe`

`v1.0.0` benzeri bir Git etiketi gönderildiğinde iki EXE otomatik olarak aynı
GitHub Release içine eklenir. Hedef bilgisayarda Python, Conda veya FFmpeg
kurulması gerekmez.

## English

Media Editor is a Turkish/English Windows video editor tailored for fast MP4
trimming, merging, overlays, encoding profiles, CPU/GPU processing and reusable
quick settings.
