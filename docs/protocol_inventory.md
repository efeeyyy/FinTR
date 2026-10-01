# Mini App WebSocket protokol envanteri

Güncelleme: 2026-09-24. Bu belge gözlenen alanları kaydeder; provider'ın tüm
varyantlarını kapsayan resmi API dokümanı değildir. Bu turda kullanıcı tarafından
paylaşılan DevTools ağaç görünümü ve redakte edilmiş shape kategorileri
karşılaştırılmış, ayrıca kullanıcının bildirdiği son başarılı default smoke
sonucu değerlendirilmiştir. Bu turda canlı socket veya secret kullanılmamış,
ham kurum satırları ve finansal rakamlar fixture'lara aktarılmamıştır.

## Kaynaklar ve kanıt sınırı

- **WS:** Kullanıcının Mini App `Network > WS Messages` gözlemleri, yönetici
  tarafından aktarılmıştır. Aşağıdaki AKD/takas alanları bu kaynağa dayanır.
  `brokerages_data` için ayrıca son smoke'ta event alındığı ve parser'ın
  `event.data.results` alanında hata verdiği kullanıcı tarafından bildirilmiştir;
  sonraki güvenli özet `event.data=object{data:string,results:missing,date:missing}`
  ile `event.data.data=string` göstermiş; daha sonraki güvenli parse özeti
  gömülü JSON'un üst düzeyini `array` olarak sınıflandırmıştır. En son kullanıcı
  bildirimi, bu nested JSON-array yanıtının mevcut typed parser'dan geçtiğini ve
  smoke'un tamamlandığını söyler. Böylece candidate alanlar yalnız o tek
  response'taki satırlar için kabul edilmiştir; tüm provider varyantları,
  isimlerin doğruluğu ve lisans/provenance doğrulanmış değildir. Ham satır/kod/ad
  paylaşılmamış veya kaydedilmemiştir.
- **JS:** Yönetici tarafından aktarılan public Mini App JavaScript incelemesi.
  [İncelenen asset: index-BYCLBwER.js](https://7k2v9x1r0z8t4m3n5p7w.com/assets/index-BYCLBwER.js)
  2026-09-23 tarihinde shell üzerinden okunmuştur; bu erişim bilgisi yönetici
  notundan alınmıştır, bu docs-only turda yeniden indirilmemiştir. Bundle
  checksum/hash'i hesaplanmamıştır; dosya adındaki sonek doğrulanmış içerik hash'i
  sayılmaz. `fetchBrokerages()` cache davranışı, brokerage handler alanları ve
  aşağıda belirtilen takas send/handler'ları bu kaynaktır. Public istemci kodu
  resmi protokol garantisi değildir. Brokerages için kullanıcı tarafından
  bildirilen canlı sonuçlar bir event ve tek başarılı nested array response'u
  içerir; JS-derived candidate alanlar son parser tarafından o response için
  kabul edilmiştir, provider-wide protokol garantisi değildir.
- **REPO:** Mevcut parser, model ve sanitize/sentetik fixture incelemesi. Offline
  testlerin geçtiği sözleşmeyi gösterir; canlı veri şekli için kanıt sayılmaz.

WS gözlem zamanı/frame kimliği ve JS içeriğinin checksum ile sürüm kimliği bu
envanter için bağımsız doğrulanmış değildir. Burada yalnız alan adları, bildirilen tipler, kaynak
etiketleri ve açık eksikler tutulur; gözlenen finansal değerler tutulmaz.

## Request ve response envanteri

`data` sütunu outbound mesajın data nesnesini anlatır; response alanları sonraki
bölümdedir. Bir satırda gözlenmeyen alan başka satırdan kopyalanmaz.

| İşlem | Outbound type | Outbound data alanları | Inbound type | Kanıt |
|---|---|---|---|---|
| AKD sorgusu | `get_akd` | `company`, `start`, `end` | `akd_data` | WS |
| AKD aboneliği | `subscribe_akd` | Tam payload bildirilmedi | `akd_snapshot` | WS; sorgu cevabından ayrı |
| Günlük takas | `get_custodian` | `company`, `date` | `custodian_data` | WS; JS'te send/handler da mevcut |
| Dönem farkı | `get_custodian_diff` | `company`, `start`, `end` | `custodian_diff_data` | WS; JS'te send/handler da mevcut |
| Takas analizi | `custody_analysis` | `symbol`, `from_date`, `to_date` | `custody_analysis_data` | WS görüntüsü; alan adları doğrulandı |
| Takas dağılımı | `get_custody_distribution` | `code` | `custody_distribution_data` | WS; JS'te send/handler da mevcut |
| Takas istatistikleri | `get_custody_stats` | `symbol`, `history: true` | `custody_stats_data` | WS; JS'te send/handler da mevcut |
| Kurum listesi | `get_brokerages` | `data` alanı yok (repo builder: yalnız `type`) | `brokerages_data` | Kullanıcı bildirimi: tek nested JSON-array response candidate typed parser'dan geçti |
| Derinlik aboneliği | Bilinmiyor | Bilinmiyor | Repo'da `depth_update` var | Request şeması açık eksik |

Şirket/sembol ve request tarihleri önceki gözlemlerde string olarak verilmiştir.
AKD aboneliğinin payload'ı veya iptal/yenileme protokolü tahmin edilmez.
`custody_analysis` görüntüsünde `to_date` değeri kısmen kesilmiştir; alan adı
doğrulanmıştır, eksik tarih tamamlanarak exact request örneği üretilmemiştir.

## Gözlenen response yapıları (WS)

`event` dış JSON mesajıdır. `event.data` envelope, `event.data.data` iç veri
nesnesidir. Yalnız bildirilen yollar listelenmiştir; her event'in aynı metadata
alanlarına sahip olduğu varsayılmaz. `number`, JSON sayısıdır; kesirli olabilir.
`null`, sıfır veya boş seri ile eşitlenmez. Bildirilmeyen tip/biçim açık bırakılır.

### AKD sorgusu: `akd_data`

- Envelope: `event.data.request_id` string; `event.data.timestamp` ve
  `event.data.processing_time_ms` number (önceki kesitte görüldü).
- `event.data.data`: `as_of` string (gözlenen biçim `HH:MM:SS`),
  `results` array, `source` string ve gözlenen etiketi `live`.
- Her results satırı: `brokerage` string; `net` nesnesinde `size`, `percentage`,
  `cost`; `total` nesnesinde `size`, `percentage`, `cost`, `volume`.
- Önceki kesitte `net.size` integer, `net.percentage/net.cost` number idi.
  Yeni bildirilen total alanlarının kesin tip/null varyantları verilmedi.
- `net` ile `total`, doğrudan alış/satış alanları değildir. Alış/satış
  türetme formülü, maliyet anlamı, yüzde ölçeği ve birimler henüz doğrulanmadı.
  `timestamp`, sorgunun başlangıç/bitiş dönemi yerine kullanılmaz; saat string'i
  olan `as_of` tek başına tarih veya timezone sağlamaz. Dönemle ilişkisi ve
  request_id korelasyonu ayrıca belirlenmeli.

`subscribe_akd` ve `akd_snapshot` da gözlenmiştir. Snapshot'ta `available`
boolean olup true/false olabilir; tam envelope ve alanın kesin yolu bu notta
verilmedi. `akd_snapshot`, `get_akd` sorgusunun asıl cevabı olan `akd_data` ile
birleştirilmemeli; aynı event adını taşıyan repo fixture'ıyla eşdeğer sayılmamalı.

### Günlük takas: `custodian_data`

- Son kullanıcı WS gözlemindeki sorgu `get_custodian` için `company` ve `date`
  taşır. Canlı yanıt zarfında `request_id` string, `processing_time_ms` ve
  `timestamp` number; iç veride ISO `date`, `results` ve `source: pg` görülmüştür.
  Yanıtta sembol yoktur; sorgu tarihi ile yanıt tarihi farklı gözlenmiştir.
- `event.data.data`: `date`, `results` array, `source` string (`pg`).
- Results satırı: `custodian` string, `value` number (kesirli değer gözlendi),
  `percentage` number.
- `percentage` **fraction/oran ölçeğinde** bildirilmiştir. Dönem farkındaki
  yüzde ölçeğiyle ham halde karşılaştırılmaz.
- Bu gözlem alanların tüm optional/null varyantlarını veya birimleri kanıtlamaz.

### Dönem farkı: `custodian_diff_data`

- Son kullanıcı WS yakalamasında `get_custodian_diff` için `company/start/end`
  gönderilmiş; yanıt zarfında `request_id` string, `timestamp` ve
  `processing_time_ms` number görülmüştür. Yanıtta sembol yoktur. İstenen
  bitiş tarihi ile iç yanıtın `end_date` tarihi farklı gözlenmiştir; bu iki
  tarih birbirinin yerine kullanılmaz.
- `event.data.data`: `start_date`, `end_date`, `results` array,
  `source` string (`pg`).
- Results satırı: `custodian` string; `difference`, `first_value`, `last_value`,
  `first_percentage`, `last_percentage`, `percentage_change` sayısal alanları.
  Değer alanları ondalıklı olabilir; integer'a kırpılmamalıdır.
- Yüzde alanları **yüzde/yüzde-puan ölçeğinde**, günlük takastaki fraction
  ölçeğinden farklı olarak bildirilmiştir. `percentage_change` için kesin
  hesap formülü ayrıca doğrulanmalıdır; alan adından göreli değişim veya
  mutlak yüzde-puan farkı formülü türetilmez.
- Tarih alanları `start_date/end_date` olarak gözlendi; repo'nun
  `first_date/last_date` adları doğrudan wire alanları sayılmaz.

### Takas analizi: `custody_analysis_data`

- Kullanıcının üç UI ağaç kopyası incelendi. `…` ve `(...)` görünüm
  kısaltmalarıdır; JSON alanı, boş dizi veya eksik gün kanıtı değildir. Kopyalar
  tam ham frame sağlamadığı için optional/null varyantları açık kalır.
- `event.data.data`: `symbol` string, `dates` tarih dizisi, `akd` ve `custody`
  ayrı nesneler; ayrıca `data_range` ve `metadata` nesneleri mevcut.
- `data_range` içinde en az `requested_from`, `requested_to`, `actual_from`
  alanları gözlendi. Tam nesne ve tüm alanların tip/biçimleri bu notta verilmedi.
  `actual_to` görülmedi; mevcut olduğu varsayılmaz.
- `metadata[date]` içinde `retail_pct`, `institutional_pct`, `investor_count`
  alanları gözlendi; paylaşılan örneklerde değerleri null. Diğer tarihlerdeki
  non-null tipler veya bu alanların her tarihte varlığı genellenmez.
- `akd[date]` array satırları: `broker_code`, `net_size`, `net_cost`,
  `net_percentage`, `total_size`, `total_cost`. Broker kodu metin alanıdır;
  açılan satırdaki bu alanlar JSON sayısı olarak görülmüştür; null/ölçek
  varyantları henüz doğrulanmadı.
- `custody[date]` array satırları: `broker_code`, `lot_count`, `percentage`.
  `lot_count` kesirli olabilir. **Örnekten çıkarım:** aynı tarih ve kurum için
  percentage'ın `custodian_diff_data.last_percentage` ile eşleşmesi, yüzde/
  yüzde-puan ölçeğini destekliyor. Bu tüm seriler için kesin protokol garantisi
  değildir; ham finansal değerler burada saklanmamıştır.
- AKD işlem dağılımı ile tarihli saklama bakiyesi ayrı tutulmalıdır. Custody
  satırında `daily_net`, `net_size` veya `cost` bulunduğu varsayılmaz; tarih/kurum
  eşleşmesi ve eksik günler için birleştirme kuralı henüz tasarlanmadı.

### Takas dağılımı: `custody_distribution_data`

- `event.data` envelope'unda `request_id` string, `timestamp` ve
  `processing_time_ms` number görülmüştür. Yanıtın kendisinde sembol yoktur.
- Kullanıcının paylaştığı DevTools ağaç kopyası, dağılım içeriğini
  `event.data.data.data` nesnesinde gösterir: `periods` tarih dizisi ve `data`
  satır dizisi. Son güvenli canlı shape özeti, `event.data.data` düzeyinde
  `data: object` ile `source: string` bulunduğunu; `periods` bulunmadığını
  doğrular. Bu iki gözlem ayrı path kanıtlarıdır, tek bir raw frame değildir.
- Önceki fixture sözleşmesinde içeriğin doğrudan `event.data.data` altında
  bulunduğu varsayımı vardır. Parser bu doğrudan yolu ve yukarıdaki belgelenmiş
  üçüncü-seviye yolu ayrı seçer; ikisi aynı anda mevcutsa belirsiz payload'ı
  reddeder. `source` yalnız tek birinde varsa o alandan okunur; iki yerde
  tekrarlanırsa reddedilir.
- Gözlenen örnekte **altı tarih** ve satır başına **altı values elemanı** var.
  Bu gözlem tüm response'lar için sabit uzunluk garantisi değildir.
- Satır: `code` string, `values` array of `number | null`,
  `percentage: number | null`.
- Yeni bir ağaç kopyasında `values` içinde kesirli JSON sayıları da görülmüştür;
  sayılar `Decimal` olarak korunur ve `values` uzunluğu `periods` uzunluğuyla
  eşleşmelidir. Bu eşleşme yuva semantiğini veya değer birimini tanımlamaz.
- Tarih/değer eşleşmesi ve ufuk anlamları doğrulanmadan altı slot beş sabit
  1/7/30/90-gün slotuna sıkıştırılmaz; null değer sıfıra çevrilmez.

### Takas istatistikleri: `custody_stats_data`

- `event.data` envelope'unda `request_id: null`, `cached: true` ve `data`
  nesnesi gözlendi. Request ID'nin her durumda string olduğu varsayılamaz;
  diğer event'lerin metadata alanları bu cached response'a dayatılamaz.
- `event.data.data`: `retail_percentage`, `institution_percentage`,
  `investor_count` serileri.
- Her seri: `format` nesnesi ve `data` array; satırlar `date`,
  `value: number | null`, `label: null` alanlarını içeriyor.
- Yüzde serilerinde `format: {decimals: 2, suffix: '%'}`; investor_count'ta
  `format: {thousand: true}` gözlendi. Bunlar görüntüleme metadata'sıdır;
  tek başına ham sayılara uygulanacak çarpan veya yuvarlama kuralı sayılmaz.
- `investor_count` serisinin başındaki birçok tarihte value null gözlenmiştir.
  Bu sıfır yatırımcı sayısı değildir. Non-null sayımın integer kısıtı, yüzde
  serilerinin ölçeği ve ilk/son geçerli gözlemi seçme politikası ayrıca doğrulanmalı.
- Uygulama durumu: gözlenen nested sözleşme ayrı `LiveCustodyStatsResponse`
  parser/model yolunda korunur; seri değerleri `Decimal | None`, görüntüleme
  `format` alanları dönüştürülmeden tutulur ve `label` null olarak doğrulanır.
  Repo fixture'ı sentetiktir; kullanıcı kaynağındaki finansal değerler veya
  tarihler kopyalanmamıştır. Legacy parser ve feature davranışı değiştirilmemiştir.

### Kontrol frame'leri (gözlenen: yalnız event adları)

Gözlem: 2026-10-01, Efe'nin tek sembollü `--show-unknown-event-types`
probu (WS). Yalnız **adlar** kaydedilir; payload alanları gözlenmemiş/kaydedilmemiştir
ve hiçbir parser tarafından kullanılmaz. Anlamlar **ad ve sıradan çıkarımdır**,
sağlayıcı dokümanıyla doğrulanmamıştır.

| Event adı | Gözlenen sıklık | Çıkarılan anlam (belirsiz) |
|---|---|---|
| `welcome` | İlk frame, bağlantı başına bir kez | Bağlantı karşılama mesajı |
| `hb` | Tekrarlayan | Heartbeat (canlılık sinyali) |
| `request_queued` | Her istekten sonra bir kez | İstek kuyruğa alındı bildirimi |

Probda başka bilinmeyen event tipi gözlenmedi. Bu üç ad, günlük çalıştırmada
fail-closed dışı tutulan kontrol frame'leri olarak
`configs/collection/daily_miniapp_v1.json` içinde `tolerated_control_event_types`
altında listelenir (ADR kararı K6); başka her bilinmeyen tip günlük çalıştırmada
sembolün kaydını engellemeye devam eder.

## Public JS bulgusu (JS; WS gözlemi değil)

`fetchBrokerages()` önce in-memory ve güncel localStorage cache'ini kontrol
eder; cache yoksa OPEN socket'e `get_brokerages` gönderir. Handler `e.data`yı
array veya nested `.data` array olarak ele alır; satırlarda `code`, `title`,
`short_title` kullanır. Bunlar **JS-derived candidate** alanlarıdır. Kullanıcının
en son bildirdiği smoke'ta `get_brokerages` gönderimi tamamlanmış, bir
`brokerages_data` yanıtı bilinen event olarak parse edilmiş ve smoke başarılı
olmuştur; böylece bu tek yanıttaki candidate satırlar mevcut typed parser'dan
geçmiştir. Bu tek gözlem tüm response varyantlarını, kurum adlarının
doğruluğunu/lisansını veya provider-wide şema garantisini doğrulamaz. Önceki
legacy parser hatası ve güvenli shape özeti de tarihsel gözlem olarak kalır.
UI/cache görünümü tek başına yeni bir WS cevabı geldiğini göstermez.

Yukarıda URL'si ve erişim tarihi kayıtlı bundle'da `get_custodian`,
`get_custodian_diff`, `get_custody_stats` ve `get_custody_distribution` için
send ve response handler'ları da bulunmuştur. Bu JS kanıtı, kullanıcı WS
gözlemlerinden ayrı tutulur; tüm metadata/optional/null varyantlarını veya
server kabulünü garanti etmez.

## Repo parser/fixture uyuşmazlıkları (REPO karşılaştırması)

- **Ortak envelope:** `custody.py` parser'ları sonuçları çoğunlukla doğrudan
  `event.data` içinde bekler; gözlenen yapı iç veriyi `event.data.data`ya koyuyor.
  Sembol/tarih alanlarını üst katmanda aramak ve bütün event'lere aynı request_id
  kuralını uygulamak güvenilir değildir.
- **AKD:** `akd_snapshot.json` ve `parse_akd_snapshot` düz `code`, `buy_lots`,
  `sell_lots`, `net_lots`, `average_cost` sözleşmesidir; gözlenen
  `brokerage/net/total/as_of/source` yapısıyla uyuşmaz. Önceki turda eklenen
  kısmi `akd_data` validator'ı yalnız request_id/metadata ve net altkümesini
  denetler; total/as_of/source'u doğrulamaz ve normalizasyon hâlâ pending'dir.
  Gerçek `akd_snapshot`ın available varyantları eski fixture'da yoktur.
- **Günlük takas:** `custodian_data.json` üstte symbol/date, altta results ve
  integer value kullanır; parser/model value için int zorunlu kılar. Fixture'daki
  yüzde ölçeği gözlenen fraction ile uyuşmaz; parser Decimal'a çevirmekle ölçeği
  normalize etmiş olmaz.
- **Dönem farkı:** `custodian_diff_data.json` ve parser `first_date/last_date`
  bekler; gözlenen `start_date/end_date` farklıdır. Değerler int zorunlu;
  `percentage_change` model/fixture'da yoktur. Yüzde ölçeği günlük takastan
  ayrı kaydedilmeli, ortak isim nedeniyle sessizce birleştirilmemelidir.
- **Analiz:** `custody_analysis_data.json` ve parser, custody satırına
  `daily_net/net_size/cost` eklenmiş birleşik şekil bekler. Gözlenen iki ayrı
  AKD/custody dizisini modellemez; `lot_count` int kısıtı kesirleri reddeder.
  Ayrı canlı model/parser gözlenen iki seriyi, kısmi `data_range` ve null
  metadata durumunu tutar; legacy fixture/assembler davranışı değişmez.
- **Dağılım:** `custody_distribution_data.json` legacy beş-slot sözleşmesidir ve
  `CustodyDistributionSnapshot`/assembler yolunda korunur. İç içe canlı zarf
  ayrı `LiveCustodyDistributionResponse` ile değişken uzunluklu `Decimal | None`
  değerleri doğrular; her satırın slot sayısı `periods` ile aynı olmalıdır.
  `features/custody.py` sabit indekslerden ufuk çıkarır; bu varsayım canlı
  şemaya taşınmaz. Sembol, yalnız aynı denemede gerçekten gönderilmiş tek
  dağılım isteğinin yerel provenance'ıdır; `request_id` korelasyonu kurulmaz.
- **İstatistikler:** `custody_stats_data.json` legacy fixture'ı serileri doğrudan
  event.data'ya koyar; null, format, label, cached ve nullable request_id canlı
  varyantlarını kapsamaz. Legacy parser/model null value reddeder ve
  `investor_count` için int bekler; yeni canlı nested sözleşme ayrı typed
  parser/model ile doğrulanır. `features/stats.py` ilk/son noktayı doğrudan
  çıkarır; eksik gözlem politikası hâlâ yoktur ve live model bu feature yoluna
  bağlanmamıştır.
- **Kurum listesi:** `brokerages_data.json`/legacy parser `date` + `data.results`
  ve `code/name` bekler; bildirilen live event'te parser `event.data.results`
  alanını bulamamıştır. Sonraki güvenli parse özeti, `event.data.data` içindeki
  JSON'un üst düzeyini array olarak sınıflandırmıştır. Daha sonraki tek başarılı
  smoke, response satırlarının JS-derived `code/title/short_title` ve string tipli
  candidate parser'dan geçtiğini göstermiştir; bu yalnız o response için kanıttır.
  Diğer provider varyantları, adların doğruluğu/lisansı ve güncellik doğrulanmaz.
  Legacy parser/fixture ayrı kalır; live fixture sentetiktir ve kullanıcı verisi
  içermez.

İlgili repo dosyaları: `src/fintr/data/parsers/akd.py`, `custody.py`,
`brokerages.py`; `src/fintr/data/models.py`; `tests/fixtures/market/`.
Canlı AKD, günlük Takas, dönem farkı, analiz, dağılım ve stats için ayrı typed yollar eklenmiştir;
bu envanter diğer veri aileleri için parser desteği iddiası değildir.

## Açık eksikler ve sonraki uygulama sınırı

- `brokerages_data` için tek başarılı nested-array response bildirilmiştir; bu
  tüm raw response varyantlarını, adların doğruluğunu/lisansını veya provider
  genelindeki şemayı doğrulamaz. Depth request payload'ı hâlâ bilinmemektedir.
- `subscribe_akd` outbound alanları, tam snapshot envelope'u ve available=false
  davranışı ayrıca doğrulanmalı. `custody_analysis` outbound alan adları ise
  görüntüden doğrulanmıştır; kesilmiş tarih değeri tamamlanmamıştır.
- Tüm event'ler için sembol/dönem korelasyonu, optional/null varyantları,
  `as_of`un tarih/timezone ilişkisi, `data_range` nesnesinin tamamı,
  `metadata[date]` non-null varyantları ve birim/ölçek tanımları tamamlanmadı.
  Gözlenen stats format nesneleri yukarıda kayıtlıdır; görüntüleme formatından
  ham veri dönüşümü türetilmez.
- `source: live/pg` ve `cached` kaynak etiketleridir; tek başına finansal doğruluk,
  güncellik, veri lisansı veya provenance garantisi değildir.

Mini App üzerinde response görülmesi, yerel collector'ın aynı veri akışını
başarıyla normalize ettiğini göstermez. Protocol-aware parser/model uyarlaması
ve sentetik regresyonlar ayrı iş olarak tasarlanmalı; bu envanter persistent
collection, dataset/training veya canlı smoke başlatma yetkisi değildir.
