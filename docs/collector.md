# WebSocket collector

## Kapsam

Collector, Veri Terminali Mini App tarafından izin verilmiş piyasa mesajlarını
kısa süreli olarak almak ve mevcut parser katmanına iletmek için tasarlanmıştır.
Authentication üretmez veya atlatmaz; kullanıcı tarafından runtime'da sağlanan
Telegram `initData` değerini yalnız bağlantı kurulurken bellekte kullanır.

Bu aşamada sürekli veri toplama ve kalıcı saklama yoktur.

## Mimari

```text
InteractiveSecretProvider
          |
          v
WebSocket transport ----> raw JSON frame
                              |
                              v
                       event validation
                              |
                              v
                         dispatcher
                              |
                              v
                       existing parsers
                              |
                              v
                     normalized objects
                              |
                              v
              MarketSnapshotAssembler (optional)
```

Transport, event doğrulama ve parser katmanları ayrıdır. `ReplayTransport`, canlı
bağlantı olmadan aynı dispatcher/parser yolunu kullanır.

## Dependency seçimi

`websockets>=17.1,<18` kullanılır. 17.1, Python 3.12 Windows wheel'i sağlar ve
Python 3.11+ destekler. İstemci güncel `websockets.asyncio.client.connect`
API'sini kullanır; bağlantı, mesaj boyutu, receive ve close timeout'ları
sınırlıdır.

Canlı Mini App handshake'i tarayıcı origin'ini gönderir. Cloudflare, bu başlık
olmadan isteği uygulamaya iletmeden HTTP 403 ile reddettiği için collector
`Origin: https://7k2v9x1r0z8t4m3n5p7w.com` başlığını açıkça gönderir. Origin
config'de query, fragment veya path kabul etmeyen ayrı bir HTTPS origin olarak
doğrulanır; auth verisi yine yalnız URL'deki geçici `init_data` parametresidir.

- Resmî paket: <https://pypi.org/project/websockets/>
- Resmî asyncio client API:
  <https://websockets.readthedocs.io/en/stable/reference/asyncio/client.html>

## Desteklenen response event'leri

- `depth_update`
- `akd_snapshot`
- `custodian_data`
- `custodian_diff_data`
- `custody_distribution_data`
- `custody_analysis_data`
- `custody_stats_data`
- `brokerages_data`

`market_update` wrapper'ı içindeki `data.type` güvenli biçimde açılır. Bilinmeyen
event crash oluşturmaz; yalnız güvenli event type debug seviyesinde loglanır.
Raw payload varsayılan olarak loglanmaz veya diske yazılmaz.

## Desteklenen request'ler

- `get_akd`
- `get_custodian`
- `get_custodian_diff`
- `get_custody_distribution`
- `custody_analysis`
- `get_custody_stats`
- `get_brokerages`

Request builder'lar sembol ve ISO tarih doğrulaması yapar ve deterministik JSON
üretir. Depth subscription request payload'ı doğrulanmış olmadığı için bilinçli
olarak uygulanmamıştır. Fiyat grafiği ve tahmin edilen diğer mesajlar da kapsam
dışıdır.

## Secret yönetimi

- `initData`, `getpass` ile hidden terminal input olarak alınır.
- Source code, config, command-line argument, fixture veya Git'e yazılmaz.
- Bağlantı URL'si yalnız `init_data=[REDACTED]` biçiminde gösterilir.
- Collector `repr()` çıktısı provider veya secret içermez.
- Terminal exception'ları secret ve query string temizlendikten sonra üretilir.
- `websockets` transport logger'ı collector içinde dışarıya yayılmaz.

Örnek redacted görünüm:

```text
wss://ws.7k2v9x1r0z8t4m3n5p7w.com/?init_data=[REDACTED]
```

## Replay modu

Replay transport, sanitize fixture frame'lerini internetsiz çalıştırır:

```python
from fintr.collector import replay_frames

results = await replay_frames(json_frames)
```

Akış canlı transport ile aynıdır: frame → validation → dispatcher → parser →
normalized object. Replay hiçbir auth bilgisi gerektirmez.

## Live smoke

Varsayılan smoke yalnız `get_brokerages` gönderir ve initData'yı hidden prompt
ile ister:

```powershell
$env:PYTHONPATH = "$PWD\src"
.\.venv\Scripts\python.exe scripts\live_collector_smoke.py
```

Başarı (exit code 0), gönderilen request türlerinden **en az birinin** beklenen
response event'inin parser tarafından normalize edilmesini gerektirir. Yalnız
handshake, `welcome`/`hb` veya istenmeyen bir known event yeterli değildir;
beklenen response alınmazsa exit code 1 döner. Kontrol frame'lerinin yalnız event
adı gösterilir; payload şemaları yorumlanmaz ve piyasa parser'larına eklenmez.

Smoke eşlemesi `scripts/live_collector_smoke.py::REQUEST_RESPONSE_EVENTS` içindedir:

| Request | Beklenen response |
|---|---|
| `get_brokerages` | `brokerages_data` |
| `get_akd` | `akd_snapshot` |
| `get_custodian` | `custodian_data` |
| `get_custody_distribution` | `custody_distribution_data` |
| `get_custody_stats` | `custody_stats_data` |
| `custody_analysis` | `custody_analysis_data` |
| `get_custodian_diff` | `custodian_diff_data` |

`--max-frames` (varsayılan 8, en fazla 50), kontrol frame'leri dahil her bağlantı
denemesindeki toplam frame sınırıdır. Eski `--max-events` aynı seçeneğin uyumlu
alias'ıdır; yalnız piyasa event'lerini saymaz. `--timeout` (varsayılan 5 s,
en fazla 30 s) her receive için ayrı sınırdır, toplam oturum deadline'ı değildir.
Collector en fazla iki retry yapar; open/close timeout'ları da sınırlıdır.
Beklenen yanıt, bounded collection tamamlandıktan sonra değerlendirilir.
Bu smoke tüm requestlerin tamamlandığını veya sembol/tarih bazında yanıt
korelasyonunu doğrulamaz; yalnız istenen response türüne ulaşıldığını doğrular.

Sembol snapshot'ları için tarihler açıkça verilmelidir:

```powershell
.\.venv\Scripts\python.exe scripts\live_collector_smoke.py `
  --symbol ASELS `
  --start-date 2026-09-01 `
  --end-date 2026-09-22 `
  --custody-date 2026-09-22
```

Bu komut `get_brokerages`, `get_akd`, `get_custodian`,
`get_custody_distribution` ve `get_custody_stats` gönderir. Full custody history
varsayılan değildir. Yalnız açık istekle eklenir:

```powershell
.\.venv\Scripts\python.exe scripts\live_collector_smoke.py `
  --symbol ASELS `
  --start-date 2026-09-01 --end-date 2026-09-22 `
  --custody-date 2026-09-22 `
  --include-custody-history `
  --from-date 2026-09-01 --to-date 2026-09-22
```

90 günden uzun history aralığı ayrıca `--allow-large-range` gerektirir. Smoke
event type dışında raw response yazdırmaz ve persistent storage kullanmaz.

## Rate ve retry güvenliği

- Aynı canonical request anahtarı varsayılan 5 saniye içinde deduplicate edilir.
- Gönderimler arasında varsayılan en az 250 ms pacing uygulanır.
- Retry varsayılan olarak en fazla 2 yeniden denemedir.
- Backoff 0.5 saniyeden başlayıp iki kat artar ve 4 saniyede sınırlandırılır.
- Sonsuz reconnect iterator kullanılmaz.

## Snapshot assembler

`MarketSnapshotAssembler`, aynı sembolün depth, AKD, custody snapshot,
distribution, history ve stats bileşenlerini birleştirir. Her bileşenin kendi
tarih/zaman alanı korunur. Aynı bileşen için daha eski intrinsic tarih/zamanla
gelen veri reddedilir; yeni receive zamanı eski veriyi yeniymiş gibi göstermez.

## Güncel sınırlamalar

- Depth subscription request protokolü bilinmiyor; yalnız response parsing var.
- Trade flow ve chart response şemaları doğrulanmadı.
- Canlı smoke otomatik test suite'ine dahil değildir ve initData gerektirir.
- Collector veriyi diske yazmaz, checkpoint veya resume sağlamaz.
- Telegram login, browser automation ve auth yenileme yoktur.
- Persistent storage, retention, lisans ve provenance manifesti sonraki aşamadır.
