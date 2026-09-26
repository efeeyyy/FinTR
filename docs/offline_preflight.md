# Çevrimiçi test öncesi çevrimdışı kontrol

Kontrol tarihi: 2026-09-25 (Europe/Istanbul). Bu sayfa sentetik fixture'lar,
parser'lar, provenance ve context doğrulama yolunun offline kontrollerini
kaydeder; canlı sağlayıcıya bağlantı veya gerçek veri doğruluğu iddiası değildir.

## Tekrar çalıştırma

Repository kökünde:

```powershell
$env:PYTHONPATH = "$PWD\src"
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
.\.venv\Scripts\python.exe -m pip check
```

Örnek bağlamın tekrar üretimi için
`scripts/build_context_example.py` çıktısı ile `docs/context_example_v0.md`
satır satır karşılaştırılır; iki taraf da 514 satırdır. Satır sonu biçimi
karşılaştırmaya katılmaz.

## Bu çalışmadaki sonuç

- 260 otomatik test: `OK`. `test_offline_live_preflight.py`, bir ağ bağlantısı
  kurmadan sentetik brokerage, AKD, günlük takas, dönem farkı, Takas Analizi,
  takas dağılımı ve takas istatistikleri mesajlarını aynı replay/dispatcher
  yolunda sınar. Kurum eşleme görünümleri yalnız anlamı doğrulanan kod
  alanlarına uygulanır. Beşli bellek-içi kanıt görünümü ve smoke aracının
  `--validate-evidence-bundle` yolu da sentetik verilerle sınanır.
- `pip check`: `No broken requirements found`. Örnek bağlam karşılaştırması:
  `PASS` (514 satır). `git diff --check`: hata yok.

## Çevrimiçi testte açık kalan kanıt sınırı

- Fixture'lar sentetiktir. Bu çevrimdışı raporun hazırlandığı sıradaki
  `brokerages_data` gözlemi, sağlayıcının tüm varyantlarını veya kurum adlarının
  doğruluğunu göstermiyordu. Sonraki toplu canlı smoke ayrı olarak
  [collector belgesinde](collector.md) kayıtlıdır.
- Replay'de gerçek gönderim kanıtı yoktur; canlı yanıtların request provenance'ı
  `pending` kalır. Ayrı sahte bağlantı testleri gönderim ve korelasyon kurallarını
  sınar, fakat gerçek sağlayıcı davranışını kanıtlamaz.
- Kurum listesi tarih taşımaz; geçmiş AKD/takas satırlarına eşlenen adların o
  tarihte geçerli olduğu iddia edilemez. Dağılım yanıtındaki `code` alanı
  kurum kodu kabul edilip otomatik eşlenmez.
- Gerçek veri lisansı, erişim izinleri, saklama politikası, bilinmeyen payload
  varyantları ve canlı oturum etkisi çevrimdışı testle doğrulanamaz.

Bu sonuçlar kısa ömürlü collector smoke'u için çevrimdışı regresyon kapısını
kapatır; sürekli toplama veya doğrudan model API entegrasyonunu doğrulamaz.

## Canlı denemeden önce

Bu sayfadaki offline testler geçtikten sonra, gerçek bir canlı
`live_collector_smoke.py` denemesi için adım adım kontrol listesi
[docs/live_check_runbook.md](live_check_runbook.md) içindedir: Mini App'i
kapatma, her planlanan komutun `--dry-run`'ını çalıştırma, önerilen
`--timeout`/`--max-frames` gerekçesi, yakalama sonrası `analyze_period.py`
ve `dev_registry.json` adımları, Mini App DevTools protokol gözlem kuralları
ve script'in sabit hata mesajlarının anlamı.
