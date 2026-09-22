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

Canlı transport, request gönderimini ilk üst düzey `welcome` event'inin sonrasına
erteler; gönderim döngüsü her bağlantı denemesinde yalnız bir kez çalışır.
Tekrarlanan `welcome` veya `hb` yeniden gönderimi tetiklemez. `hb`, `error`,
`banned` ve nested `market_update` içindeki bir `welcome` hazır sinyali sayılmaz.
Welcome payload'ına özel şema eklenmez veya payload loglanmaz; event mevcut
dispatcher'dan unknown result olarak geçer. Welcome gelmezse receive timeout'u
veya toplam frame sınırı içinde request gönderilmeden dönülür. Bu sınırlar
welcome beklenirken de geçerlidir; retry/dedup davranışı değişmemiştir.

Welcome-sonrası gönderim kullanıcı tarafından canlı denendi; beklenen response
yine alınmadı. Bu sıra değişikliği sorunu çözmüş sayılmaz. Offline testler yalnız
gönderim sırasını, tek seferlik tetiklemeyi ve bekleme sınırlarını doğrular.

## Canlı doğrulama durumu ve protokol sınırları

Aşağıdaki canlı sonuçlar kullanıcı/yönetici tarafından bildirilen gözlemlerdir;
bu dokümantasyon güncellemesinde yeni bağlantı kurulmadı. Public Mini App JS
bulguları da yönetici tarafından aktarılan kaynak incelemesidir; canlı response
örneği veya sunucu protokol garantisi değildir.

- **Transport:** Origin düzeltmesinden sonra WebSocket handshake başarılıdır;
  bildirilen denemelerde HTTP 403 giderilmiştir. `welcome` ve `hb` alınması,
  piyasa request'inin sunucuda kabul edildiğini kanıtlamaz.
- **Son smoke:** Mini App kapalıyken 35 frame sınırında bir `welcome` ve 34 `hb`
  alınmış, `brokerages_data` alınmamış ve araç `Smoke failed` ile sonlanmıştır.
  Önceki 8 frame sınırındaki ve welcome-sonrası gönderim denemelerinde de
  beklenen response yoktur. Oturum çakışması son gözlemi açıklamıyor; asıl neden
  henüz belirlenmedi.
- **Gönderim kanıtının sınırı:** Repo kodu welcome sonrasında
  `{"type":"get_brokerages"}` için `websocket.send()` çağrısını await eder.
  Bu istemci davranışıdır; sunucunun request'i kabul ettiği, işlediği veya yanıt
  verdiği doğrulanmamıştır. Frame sınırını artırmak ya da welcome sonrasına
  ertelemek bu denemelerde beklenen response'u sağlamamıştır.
- **Client-side cache:** Aktarılan public JS'teki `fetchBrokerages()` önce
  in-memory ve güncel localStorage cache'ini kontrol eder. Yalnız cache yoksa
  OPEN socket'e `{"type":"get_brokerages"}` gönderir. Mini App'te kurum
  listesinin görünmesi tek başına yeni bir WebSocket response'u kanıtlamaz;
  cache davranışı da collector'da yanıt gelmemesinin nedeni olarak doğrulanmış
  değildir.

Brokerage şemasında ayrıca çözülmemiş bir uyuşmazlık vardır:

| Kanıt düzeyi | Beklenen biçim |
|---|---|
| Aktarılan public JS `brokerages_data` handler'ı | `e.data` array veya onun nested `.data` array'i; satırlarda `code`, `title`, `short_title` |
| Repo fixture/parser sözleşmesi | `date` + `data.results` array'i; satırlarda `code`, `name` |
| Alınmış canlı `brokerages_data` frame'i | Yok; gerçek response şeması doğrulanmadı |

Repo beklentisi `tests/fixtures/market/brokerages_data.json` ve
`src/fintr/data/parsers/brokerages.py` ile doğrulanabilir. Fixture canlı kaynaktan
doğrulanmış bir şema olarak sunulmamalıdır. Public JS ile uyuşmazlık, ileride
response gelirse parser uyumluluğu riski taşır; şu an event'in hiç gelmemesini
açıklamaz. Bu kayıt kapsamında parser, fixture veya request eşlemesi değiştirilmedi.

Bu provider yolu uçtan uca piyasa verisi erişimi, kaynak doğruluğu, persistent
collection veya dataset/training için hazır değildir. Offline test başarısı
canlı şema, veri bütünlüğü, lisans/izin ve provenance doğrulamasının yerine geçmez.
Kalıcı toplama ve veri kullanımından önce gerçek response sözleşmesi ile
kaynak/izin/provenance koşulları doğrulanmalıdır.

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

Aşağıdaki liste kod/fixture düzeyindeki desteği gösterir; canlı sağlayıcıyla
uçtan uca doğrulanmış event listesi değildir.

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

Canlı sağlayıcı aynı hesap/initData için tek aktif WebSocket oturumuna izin verir.
Collector bağlantısı açık Telegram Mini App oturumunu kapatabilir ve Mini App'te
“Başka Cihazdan Giriş Yapıldı” uyarısına yol açabilir. Eşzamanlı kullanım
desteklenmez. Bu nedenle live smoke her çalıştırmada açık
`--allow-session-takeover` onayı ister; flag yoksa initData istemeden ve ağa
bağlanmadan exit code 2 ile durur.

Güvenli çalışma sırası:

1. Telegram Mini App açıkken gerekli `initData` değerini geçici olarak alın.
2. Mini App'i kapatın; aynı oturumu eşzamanlı kullanmayın.
3. Collector'ı `--allow-session-takeover` flag'iyle başlatın ve hidden prompt'a
   initData'yı girin.
4. Collector bittikten sonra Telegram'da Mini App'i yeniden açarak yeni oturum
   başlatın.

Varsayılan smoke yalnız `get_brokerages` gönderir:

```powershell
$env:PYTHONPATH = "$PWD\src"
.\.venv\Scripts\python.exe scripts\live_collector_smoke.py `
  --allow-session-takeover
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
  --allow-session-takeover `
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
  --allow-session-takeover `
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
