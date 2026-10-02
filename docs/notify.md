# Telegram gözlem bildirimi (`fintr.notify`)

Aşama 2'nin ikinci adımı: `scripts/scan_observations.py` tarafından
kaydedilmiş gözlem dosyasını (`data/interim/scanner/<as_of>/observations.json`,
şema `fintr_scanner_observations_v1` ya da `_v2` -- `_v2` yalnız tarayıcı
`--daily-root` ile çalıştırıldıysa, bkz. `docs/scanner.md`) okuyup Efe'nin
kendi Telegram botuyla, yalnız Efe'nin sohbetine düz metin bildirim olarak
gönderir. `fintr.notify.format.SUPPORTED_OBSERVATIONS_SCHEMAS` her iki
şemayı da kabul eder; eski (`_v1`, `--daily-root` öncesi ya da onsuz
üretilmiş) dosyalar aynen okunmaya devam eder.

## Bu bir yatırım tavsiyesi değildir

Bildirim, tarayıcının ürettiği gözlemin aynen özetidir. `docs/scanner.md`'deki
sınırlar burada da geçerlidir: **getiri, hedef, puan, sıralama, stop seviyesi,
pozisyon büyüklüğü veya serbest metin al/sat önerisi üretmez**; tek istisna
aşağıdaki "Beklenen yön etiketi" bölümüdür. Her mesajın sonunda sabit Türkçe
uyarı metni ve `validation_status` yer alır. Mesaj yalnız `condition_met: true` olan
gözlemleri, tazelik uyarılarını (T+2, eksik veri), bastırılan gözlem
sayılarını (nedenlerine göre) ve -- yalnız `--daily-root` ile üretilmiş
dosyalarda -- sembol başına tek satırlık bir veri durumu satırı listeler.

### Beklenen yön etiketi (Efe kararı, 2026-10-02; yalnız kişisel kullanım)

Varsayılan olarak her `condition_met: true` satırı, ilgili dondurulmuş kuralın
hipotez yönüyle etiketlenir:

```
AKFIS H2: <kural açıklaması> — vwap_proxy_vs_close=... — beklenen yön: yukarı (al yönlü)
AKFIS H1: <kural açıklaması> — vwap_proxy_vs_close=... — beklenen yön: aşağı (sat yönlü)
```

- **Kaynak:** dondurulmuş spec'in `preregistration.predicted_direction` alanı
  (`positive` → "yukarı (al yönlü)", `negative` → "aşağı (sat yönlü)"). Alan,
  tarayıcının hash doğrulamalı yükleyicisiyle (`fintr.scanner.rules.load_scanner_rules`,
  `fintr.notify.direction.load_rule_directions`) `configs/scanner/rules_v1.json`
  üzerinden okunur (`--rules-file` ile değiştirilebilir). Kural çözülemezse, spec
  hash'i uymazsa ya da alan tam olarak `positive`/`negative` değilse satır etiketsiz
  kalır ve `— yön bilinmiyor` işareti taşır; yön asla tahmin edilmez.
- **Uyarı:** etiket içeren her mesaj bir kez
  "Uyarı: yön etiketi doğrulanmamış ön kayıtlı hipotezdir (holdout testi 2027 Nisan);
  yatırım tavsiyesi değildir." satırını taşır (mesaj bölünürse etiketli her parça).
- **Alt bilgi:** etiketli mesajda sabit metin "…kişisel kullanım; doğrulanmamış hipotez
  yönü; yatırım tavsiyesi, niyet veya manipülasyon iddiası değildir…" olur (tarayıcının
  "al/sat sinyali değildir" metni etiketle çeliştiği için yerine geçer); etiketsiz
  mesajda önceki metin aynen kalır. `Durum:` satırı değişmez.
- **Değişmeyenler:** kural, eşik ve spec değişmez; tarayıcı çıktı şeması değişmez;
  etiket gözleme yeni bilgi eklemez, yalnız spec'te kayıtlı yönü ifade eder. Getiri,
  puan, sıralama, hedef fiyat, stop seviyesi, pozisyon büyüklüğü eklenmez. Holdout
  testi ve K4 geçerlidir; etiket bir avantaj iddiası değildir.
- **Kapatma:** `--no-direction-label` önceki metni birebir geri getirir (etiket, işaret
  ve ek uyarı yok). Karar kaydı: `docs/prereg/DECISIONS.md` 2026-10-02.

### Veri durumu satırı (yalnız `--daily-root` çıktısında)

Gözlem dosyasının `symbols.<SYM>.composition` alanı varsa (tarayıcı
`--daily-root` ile çalıştırılmışsa), o sembol için şu biçimde tek bir
satır eklenir:

```
AKFIS veri durumu (2026-09-08..2026-09-25): boşluk=1, kesinleşmemiş gün=2, revize gün=1
```

Üç sayı da `docs/scanner.md`'deki `composition` bloğundan gelir ve
`composition.window` aralığıyla (günlük girdilerin en erken sunucu tarihi..
as_of, iki uç dahil) sınırlıdır: `boşluk` = `composition.gaps.in_window_count`,
`kesinleşmemiş gün` = `composition.unsettled.in_window_day_count` (tekil gün
sayısı, aile bazlı değil), `revize gün` = `composition.revisions
.in_window_day_count`. Pencere alanları olmayan eski `_v2` dosyasında satır
tüm dönem toplamlarına düşer ve `(tüm dönem)` diye etiketlenir; `window: null`
(günlük girdi yok) ise satır `veri durumu: pencere yok (günlük kayıt yok)`
olur. Bu satır yalnız sayı taşır; hiçbir
değer, iddia ya da al/sat ifadesi eklemez -- mevcut yasak kelime testleri
(`tests/test_notify.py`; yalnız iki etiket ifadesine izin verir) bu satırı da kapsar. `composition` alanı yoksa
(varsayılan, `--daily-root` verilmeden üretilmiş her dosya) bu satır hiç
eklenmez.

### Analiz notu (`--llm-note-dir`, Efe kararı 2026-10-02; varsayılan kapalı)

`--llm-note-dir data/interim/llm_notes` verilirse, deterministik mesaj(lar)ın
ardından **ayrı bir mesaj** olarak Opus 5.5 analiz notu eklenir
(`scripts/llm_note.py`, `src/fintr/llm_note/`; tasarım:
`docs/architecture.md`, karar: `docs/prereg/DECISIONS.md`). Bayrak verilmezse
bildirim metni ve alıcıya giden mesajlar bayt düzeyinde öncekiyle aynıdır.

```
Opus 5.5 analiz notu — doğrulanmamış hipotez değerlendirmesi, yatırım tavsiyesi değildir
as_of: 2026-09-30
Uyarı: yön etiketi doğrulanmamış ön kayıtlı hipotezdir (holdout testi 2027 Nisan); yatırım tavsiyesi değildir.

AKFIS H2 (beklenen yön: yukarı (al yönlü)) — değerlendirme: destekliyor
• <doğrulanmış gerekçe metni>
...
Veri notları:
• <veri sınırlamaları>

Rakamlar ve kanıt yolları girdi paketiyle kodla denetlendi; model çıktısı deterministik değildir; ...
Kayıt: data/interim/llm_notes/2026-09-30/attempt-001
Durum: preregistered_hypothesis_holdout_test_pending
```

- **Ne içerir:** her `condition_met` gözlem için dondurulmuş kuralın yönünü veri
  `destekliyor` / `zayıflatıyor` / `belirsiz` ve kısa, kanıt yollu gerekçeler.
  Değerlendirme dışında yeni finansal ifade eklemez; al/sat emri, hedef, stop,
  pozisyon, getiri tahmini veya puan üretmez. Yön etiketi, deterministik
  mesajdaki aynı iki sabit ifadedir ve `--no-direction-label` ile notta da
  kalkar (uyarı satırı ve etiket birlikte).
- **Doğrulama:** not yalnız kodla doğrulandıysa gösterilir (şema, izinli değer,
  kapsam, kanıt yolları, yasak ifade, metindeki her rakam/tarih girdi paketinde).
  Gösterim sırasında kayıt bütünlüğü yeniden denetlenir (paket sha256 + yeniden
  doğrulama); değişmiş bir not gösterilmez.
- **Not yoksa tek satır durum** (ayrı mesaj): `Opus 5.5 analiz notu: analiz notu
  alınamadı (<kategori>).` (model reddi, abonelik limiti, zaman aşımı, CLI
  hatası, paket çok büyük), `... doğrulamadan geçemedi (<kategori>).` (reddedilen
  not; içeriği gönderilmez), `... bu as_of için not yok (adım çalıştırılmadı).`
  veya `... bu taramada gözlem yok; not üretilmedi.` Reddedilen/eksik not
  deterministik mesajı hiçbir zaman engellemez.
- **Receipt:** notla birlikte gönderilen mesajlar aynı makbuzdadır
  (`message_sha256` hepsini kapsar; yalnız bu bayrakla `llm_note_messages`
  alanı eklenir). Mevcut tekrar gönderim engeli aynen geçerlidir: makbuz varsa
  not sonradan üretilse de yeniden gönderilmez (`--resend` bilinçli karar ister).
- **Veri Anthropic'e gider:** ham AKD/takas verisinin üçüncü tarafa gönderimi
  Efe'nin kararıdır; Mini App lisans beyanının bunu kapsadığı doğrulanmadı.

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
