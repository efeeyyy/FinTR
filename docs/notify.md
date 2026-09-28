# Telegram gözlem bildirimi (`fintr.notify`)

Aşama 2'nin ikinci adımı: `scripts/scan_observations.py` tarafından
kaydedilmiş gözlem dosyasını (`data/interim/scanner/<as_of>/observations.json`,
şema `fintr_scanner_observations_v1`) okuyup Efe'nin kendi Telegram botuyla,
yalnız Efe'nin sohbetine düz metin bildirim olarak gönderir.

## Bu bir sinyal değildir

Bildirim, tarayıcının ürettiği gözlemin aynen özetidir. `docs/scanner.md`'deki
sınırlar burada da geçerlidir: **getiri, hedef, puan, sıralama veya al/sat
üretmez**; her mesajın sonunda tarayıcının sabit Türkçe uyarı metni ve
`validation_status` yer alır. Mesaj yalnız `condition_met: true` olan
gözlemleri, tazelik uyarılarını (T+2, eksik veri) ve bastırılan gözlem
sayılarını (nedenlerine göre) listeler.

## 1. BotFather ile bot açma

1. Telegram'da `@BotFather` ile sohbet başlat.
2. `/newbot` komutunu gönder; bot için bir görünen ad ve `_bot` ile biten
   benzersiz bir kullanıcı adı iste (ör. `fintr_efe_scanner_bot`).
3. BotFather sana bir **token** verir (`123456789:AA...` biçiminde). Bu
   token'ı kimseyle paylaşma; ekran görüntüsü alma, commit'e ekleme, loga
   yazma. Yalnızca aşağıdaki ortam değişkenine koyacaksın.

## 2. Botla sohbet başlatma ve chat id bulma

1. Telegram'da yeni botunu ara (kullanıcı adıyla) ve sohbeti başlatmak için
   `/start` gönder (ya da herhangi bir mesaj yaz).
2. Tarayıcıda şu adresi aç (TOKEN'ı kendi token'ınla değiştir):
   `https://api.telegram.org/botTOKEN/getUpdates`
3. Dönen JSON'da `result[0].message.chat.id` alanı senin chat id'ndir
   (pozitif veya negatif bir tam sayı). Bunu not al.
4. `getUpdates` boş dönerse, botla az önce mesajlaştığından emin ol ve
   sayfayı yenile.

Bot yalnız bu chat id'ye yazar; başka hiçbir sohbete veya kanala mesaj
göndermez (kod bunu zorlamaz, ama `FINTR_TELEGRAM_CHAT_ID` yalnız senin
chat id'ni içermelidir).

## 3. Ortam değişkenlerini PowerShell'de ayarlama

```powershell
$env:FINTR_TELEGRAM_BOT_TOKEN = "123456789:AA...senin-token'ın..."
$env:FINTR_TELEGRAM_CHAT_ID = "123456789"
```

Bu değişkenler yalnız o PowerShell oturumunda geçerlidir; token hiçbir
dosyaya yazılmaz. Kalıcı yapmak istersen `[Environment]::SetEnvironmentVariable`
kullanabilirsin, ama token'ı bir script dosyasına gömme.

## 4. Önce dry-run, sonra `--send`

Varsayılan davranış **dry-run**: mesaj(lar) yalnız stdout'a basılır, ağ
hiç kullanılmaz. Gerçek gönderim için `--send` açıkça gerekir.

```powershell
$env:PYTHONPATH = "$PWD\src"

# 1) Önce dry-run: mesajı gözden geçir, hiçbir ağ çağrısı yapılmaz.
.\.venv\Scripts\python.exe scripts\notify_observations.py --as-of 2026-09-25

# 2) Sorun yoksa gerçekten gönder.
.\.venv\Scripts\python.exe scripts\notify_observations.py --as-of 2026-09-25 --send

# Bu taramada condition_met=true gözlem yoksa hiçbir şey göndermek istersen:
.\.venv\Scripts\python.exe scripts\notify_observations.py --as-of 2026-09-25 --send --skip-empty

# Aynı as_of için receipt zaten varsa ve yine de tekrar göndermek istersen
# (üzerine yazmaz, yeni bir receipt dosyası oluşturur):
.\.venv\Scripts\python.exe scripts\notify_observations.py --as-of 2026-09-25 --send --resend
```

Başarılı bir `--send` sonrası `data/interim/notify/<as_of>/receipt.json`
yazılır: girdi dosyasının SHA-256'sı, gönderilen mesaj metinlerinin
SHA-256'sı, gönderim zamanı ve Telegram `message_id`'leri. Token veya chat
id receipt'e hiçbir zaman yazılmaz. Aynı `as_of` için receipt zaten varsa
ikinci `--send` reddedilir (`--resend` verilmedikçe); receipt dosyasının
üzerine hiçbir zaman yazılmaz.

## Güvenlik notları

- Token asla paylaşılmaz, loglanmaz, receipt'e veya hata mesajına
  yazılmaz; yalnız `FINTR_TELEGRAM_BOT_TOKEN` ortam değişkeninden okunur.
- Bot yalnız `FINTR_TELEGRAM_CHAT_ID` ile belirttiğin chat'e yazar.
- Mesaj düz metin gönderilir (`parse_mode` kullanılmaz); Telegram tarafında
  biçimlendirme (kalın, link vb.) uygulanmaz.
- HTTP zaman aşımı 15 saniyedir. Telegram 429 (hız sınırı) döndürürse ve
  `retry_after` 30 saniyeden kısaysa tek bir yeniden deneme yapılır; aksi
  halde bildirim açık bir hatayla durur (siz tekrar çalıştırırsınız).
- Token'ı bir yere yanlışlıkla yapıştırdıysan BotFather'da `/revoke`
  ile iptal edip yeni token alabilirsin.
