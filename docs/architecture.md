# FinTR-LLM architecture

## Tasarım ilkeleri

- Ham piyasa verisi, normalize kayıt, deterministik özellik ve LLM yorumu ayrı
  katmanlardır.
- Sembol, tarih/zaman ve mesaj türü dönüşüm boyunca korunur.
- Parser'lar eksik alanı veya yanlış tipi sessizce düzeltmez.
- AKD ve takas farklı zaman/kapsam semantiğine sahip veri aileleridir.
- Development pilotları bağımsız evaluation vakası olarak kullanılmaz.
- Model yorumu, repository içindeki veri toplama ve hesaplama kodundan ayrı
  yürütülür.

## Hedef akış

```text
WebSocket / raw market data
            |
            v
          parser --------------> açık şema/tip hataları
            |
            v
     normalized schema --------> stdlib dataclasses + symbol/date provenance
            |
            v
       feature engine ---------> deterministik, sürümlü formüller
            |
            v
       context builder --------> kaynak / hesap / eksik veri ayrımı
            |
            v
       bounded context -------> Efe + daha güçlü yönetici model
            |
            v
        evaluation ------------> insan incelemesi + rubric puanlaması
```

Bounded WebSocket collector uygulanmıştır. Kullanıcının canlı denemelerinde
handshake başarılıdır. Bildirilen 2026-09-24 toplu smoke'ta aynı denemede
brokerage, AKD, günlük Takas, Takas dağılımı ve Takas istatistikleri istekleri
gönderilmiş; beş yanıt da normalize edilip sıkı kabul kontrolünden geçmiştir.
Bu yalnız bildirilen oturumdaki transport/parser davranışını gösterir; tüm
sağlayıcı varyantları, finansal değerler veya isimlerin doğruluğu/lisansı
kanıtlanmış değildir. `tests/fixtures/market/` altındaki sanitize
mesajlar sentetiktir ve canlı sağlayıcı şemasının genelliğini kanıtlamaz.
Brokerage kanıt sınırı için [collector sınırları](collector.md) belgesine bakın. Kullanıcının
Mini App ve kısa collector denemelerinde gözlenen AKD/takas yapıları
[protokol envanterindedir](protocol_inventory.md); canlı typed yanıtların
feature/context katmanına bağlandığı anlamına gelmez.

## Modül sorumlulukları

- `fintr.data.models`: normalize ve immutable piyasa veri sözleşmeleri.
- `fintr.data.parsers`: mesaj türüne özgü doğrulama ve normalize etme.
- `fintr.collector`: runtime secret provider, bounded WebSocket/replay transport,
  event dispatcher, request builders ve freshness-aware snapshot assembler.
- `fintr.features`: LLM'den bağımsız sayısal özellik hesapları.
- `fintr.inference.context`: sınırlı, provenance taşıyan structured context ve
  deterministik text renderer.
- `fintr.inference.prompts`: model çağrısından bağımsız analysis prompt şablonu.
- `fintr.evaluation`: piyasa mikroyapısı rubric'i ve deterministik kontroller.

## Veri modelleri

Fiyat, maliyet ve yüzdeler `Decimal`; lot, değer ve yatırımcı adedi `int`; tarih
alanları `date`/`datetime` olarak normalize edilir. Koleksiyonlar immutable
`tuple` biçimindedir. `MarketSnapshot`, aynı sembole ait opsiyonel veri ailelerini
bir araya getirir; tarih/kapsam uyumunu context builder ayrıca doğrulayacaktır.

Bu paragraf mevcut repo modelini tarif eder. Yeni WS gözlemlerindeki kesirli
takas değerleri/lot_count, nullable seriler ve altı dönem bu modelle tam uyumlu
değildir. Günlük takasın fraction ölçeğiyle dönem farkının yüzde ölçeği ayrı
temsil edilmelidir; docs-only envanter güncellemesi modeli değiştirmemiştir.

### Kısmi canlı AKD sözleşmesi kararı

İlk kısmi kanıt sırasında problem: kullanıcı Mini App üzerinde `get_akd` için `akd_data` envelope'unu
gözlemledi; mevcut `akd_snapshot` fixture'ı bu canlı sözleşmeyi temsil etmiyor.
O aşamada tam kurum satırı ve sembol/dönem bağlantısı bilinmiyordu. Eski modele zorla
eşlemek alış/satış, maliyet ve tarih alanlarında kanıtsız değer üretirdi; event'i
unknown bırakmak ise doğrulanan bilgiyi kaybederdi.

Karar: bilinen envelope/alan tiplerini ayrı validator ile kontrol et;
dispatcher `known=True`, `normalized=None`, `normalization_pending=True` döndürsün.
Bu, tamamlanmış bir `AKDSnapshot` değildir; feature/context/assembler'a verilmez
ve tek başına smoke başarısı üretmez. Bilinmeyen alanlar yorumlanmaz, raw payload
ve request_id tutulmaz/loglanmaz. Tam şema gelene kadar eski snapshot parser'ı
offline sözleşme olarak korunur. Sonuç: event tanıma ile finansal normalizasyon
başarısı ayrılır; yeni bir piyasa veri modeli veya tahmini request eklenmez.

Sonraki gözlemde `as_of`, `source`, `brokerage/net/total` satırı ve ayrı abonelik
snapshot'ı belirlendi. Bu tarihsel kısmi kararın ardından sorgu yanıtı için
aşağıdaki ayrı typed model/parser eklendi; bağlamsız replay sonucu yine pending
kalır. Legacy `AKDSnapshot` ile birleştirilmedi.

### AKD sorgu yanıtı için ilk typed dilim

Problem: gözlenen `akd_data` alanları kısmi validator'da kayboluyor; buna karşılık
yanıtta sembol ve sorgu tarih aralığı yok. Eski `AKDSnapshot`'a dönüştürmek
kanıtsız alış/satış, tarih ve birim üretir. Karar: doğrulanmış alanları ayrı,
immutable `LiveAKDResponse` modelinde koru; sembol/başlangıç/bitiş yalnız aynı
bağlantı denemesinde gerçekten gönderilen **tek** `get_akd` isteği varsa ayrı
`local_sent_request` provenance'ına bağlansın. Birden çok, hiç veya önceki
retry'dan kalan istek yanıtı doğrulanmış bağlam sayılmaz. `request_id` sunucu
tarafı korelasyon kanıtı olarak kullanılmaz. Sonuç: parser/replay bağlamsız
AKD'yi typed fakat pending tutar; collector'ın güvenli bağladığı yanıt smoke
başarısı olabilir. Feature/context/assembler ve diğer piyasa aileleri değişmez.

### Collector hata sınıflandırması

Problem: her exception'ı retry etmek ilk decode/parse hatasını gizleyip ikinci
denemede rate-limit atlaması gibi yanıltıcı bir iz bırakabilir. Karar: yalnız
geçici transport hatalarını sınırlı retry kapsamına al; decode/parse ve
beklenmeyen programlama hatalarında hemen dur. Deneme, aşama, izinli kategori
ve doğrulanmış şema yolu dışında hata metni/payload raporlanmaz. Böylece ilk
başarısız deneme görünür, fakat yanıt alınmış olduğu kanıtsız iddia edilmez.

Dinamik tarih anahtarlı analiz hatalarında alan yolu yalnız sabit şema adları
ve `[date]`/`[index]` yer tutucularıyla gösterilir. Beklenen ve gelen JSON türü
parser'ın yapılandırılmış hata metadata'sından alınır; ham değer/hata mesajı
çıktıya aktarılmaz. Bilinmeyen metadata non-null varyantı ayrı kategori olarak
bildirilir; şema bu teşhis nedeniyle gevşetilmez.

### Günlük Takas canlı yanıtı

Gözlenen `custodian_data` yanıtı `event.data.data.results` içinde kesirli
değerler ve ayrı bir yanıt tarihi taşıyor; legacy fixture ise düz zarf, sembol
ve tam sayı değer bekliyor. Bu yüzden canlı yanıt ayrı immutable model/parser
ile işlenir. Legacy biçim yalnız açık düz zarf işaretleriyle seçilir; eksik
canlı alanlar legacy parser'a düşmez. Sembol ve istenen tarih yalnız aynı
bağlantıda gerçekten gönderilmiş tek `get_custodian` isteğinin yerel kaydıdır;
yanıt tarihi değiştirilmez. Birim veya yüzde dönüşümü yapılmaz.

### Takas dönem farkı canlı yanıtı

`custodian_diff_data` canlı yanıtı iç içe zarf, `start_date/end_date` ve kesirli
sayısal alanlar kullanır; legacy fixture düz zarf ile `first_date/last_date`
bekler. Canlı yanıt ayrı immutable model/parser'a ayrılır ve legacy biçim
yalnız açık düz zarf işaretleriyle seçilir. Yanıttaki dönem korunur; sorgudaki
sembol ve istenen dönem yalnız tek gerçekten gönderilmiş isteğin yerel
provenance'ıdır. Yüzde alanları günlük Takas alanlarına dönüştürülmez.

### Takas analizi canlı yanıtı

Gözlenen `custody_analysis_data` ağaç görünümü işlem dağılımı `akd[date]` ile
sahiplik bakiyesi `custody[date]` serilerini ayrı gösterir. Legacy `CustodyHistory`
tek satırda ikisini birleştirir ve canlı custody satırında görülmeyen `daily_net`
ile `cost` ister. Karar: bilinen alanları ayrı immutable canlı modelde tut;
gösterimdeki `…` kısaltmalarını veri sayma ve eksik gün doldurma. Metadata'da
yalnız doğrulanan null durumu modellenir; non-null varyant henüz şema dışıdır.
`data_range` için yalnız gözlenen `requested_from/requested_to/actual_from`
alanları yorumlanır, `actual_to` varsayılmaz. Canlı yanıtın sembolü, tek
gerçekten gönderilmiş isteğin yerel sembolüyle eşleşmeden başarı sayılmaz.

### Takas dağılımı canlı yanıtı

Gözlenen `custody_distribution_data` iç içe envelope, nullable/kesirli values
ve `periods` ile eşleşen fakat uzunluğu sabit olmayan slot dizisi kullanır;
legacy fixture ise beş integer slotlu düz şekildir. Karar: legacy parser/model ve
assembler davranışını değiştirme, canlı response için doğrudan ve DevTools
ağacının gösterdiği üçüncü-seviye payload yollarını ayrı immutable model/parser
ile doğrula. İki path aynı anda mevcutsa sessiz seçim yerine hata ver. Legacy
modeli nullable/değişken slotlarla genişletmek, mevcut feature/assembler'ın sabit
slot beklentisini belirsizleştirirdi. Sembol yanıtta bulunmadığı için yalnız tek
gerçekten gönderilmiş isteğin yerel provenance'ı eklenir; `request_id` üzerinden
korelasyon veya değer birimi/ufuk anlamı türetilmez.

### Takas istatistikleri canlı yanıtı

Gözlenen `custody_stats_data` canlı zarfı legacy `CustodyStats` biçiminden
farklıdır: `request_id` null olabilir, `cached` bulunur ve üç seri içteki
`data` nesnesinde görüntüleme amaçlı `format` metadata'sıyla taşınır. Bazı
gözlemlerin `value` alanı null'dır ve `label` null olarak görülmüştür. Karar:
legacy parser/model ve `features/stats.py` tüketim davranışını değiştirmeden,
canlı zarfı ayrı immutable `LiveCustodyStatsResponse` ve strict parser ile
doğrula. Format alanları olduğu gibi saklanır; sayılar yuvarlanmaz, ölçeklenmez
ve null sıfıra çevrilmez. Sembol ve `history` yalnız aynı bağlantı denemesinde
tamamlanan tek `get_custody_stats` gönderiminden yerel provenance olarak eklenir;
`request_id` veya `cached` yanıt korelasyonu sayılmaz. Birden çok/hiç gönderim
ve rate-limit atlamasında response pending kalır.

### Brokerage kod-etiket indeksi

Problem: Kullanıcının ilk bildirdiği başarılı `brokerages_data` yanıtı, mevcut
candidate typed parser'dan geçmişti; sonraki kullanımda kodu etikete çevirmek
gerekebilir. Tekil gözlemler tarih, kaynak lisansı, provider-wide tutarlılık veya
başka event'lerle eşdeğerlik sağlamaz.

Alternatifler: veriyi otomatik `MarketSnapshot`/context'e eklemek, kalıcı ve
güncel bir referans tablosu kurmak ya da yalnız verilen response için saf bir
in-memory index sunmak. İlk iki seçenek zaman/provenance ve saklama kararlarını
gerektirir.

Karar: `LiveBrokerageIndex` yalnız `LiveBrokeragesResponse` üzerinden kurulur ve
exact code lookup yapar. Bilinmeyen kod `unresolved`; aynı kod için aynı
title/short-title çifti yinelenirse tek eşleşme, farklı etiket çiftleri varsa
`ambiguous` döner. Kod/ad/etiket değerleri repr ve hata metinlerine çıkarılmaz.

Sonuç: resolver bağımsız, değiştirilemez ve process belleğiyle sınırlıdır; tarih,
lisans, alias/case normalizasyonu, cross-event eşleştirme veya doğruluk iddiası
eklemez. `MarketSnapshot`, feature, context ve persistent storage bağlantıları
bilinçli olarak ertelenmiştir.

### Canlı AKD kurum adı bağlantısı

Problem: `akd_data` kurum kodları taşır, fakat kurum adları ayrı bir
`brokerages_data` yanıtından gelir. Parser'a ad yazmak sayısal AKD verisini ve
bağımsız kaynakların sınırını karıştırır.

Alternatifler: AKD modelini doğrudan değiştirmek, eksik kodlar için ad tahmin
etmek veya iki typed yanıtı isteğe bağlı bir görünümde ilişkilendirmek.

Karar: `resolve_live_akd_brokerages` verilen tek `LiveAKDResponse` ile tek
`LiveBrokerageIndex` arasında exact-code, bellekte bir bağlantı kurar. Her
satır orijinal AKD satırını ve `resolved` / `unresolved` / `ambiguous` sonucunu
ayrı tutar. İsim yalnız kesin tek eşleşmede kullanılabilir; girdi yanıtları,
sıra, sayılar ve yerel istek provenance'ı değiştirilmez. Görünüm, hangi AKD
yanıtı ve hangi kurum indeksiyle üretildiğini referans olarak korur.

Sonuç: Bu yalnız AKD için çağrıldığında üretilen bir ara çıktıdır; collector,
kalıcı depolama, legacy `MarketSnapshot`, feature ve LLM context yoluna henüz
otomatik bağlanmaz. Dizin yanıtı tarihsiz olduğundan adın geçerlilik dönemi
veya sağlayıcı genelindeki doğruluğu iddia edilmez.

### Canlı takas kurum adı bağlantısı

Problem: `custodian_data` ve `custodian_diff_data` kurum kodları taşır; kurum
adları ayrı ve tarihsiz bir `brokerages_data` yanıtından gelir. AKD, günlük
takas ve dönem farkı farklı kaynak/zaman kapsamları olarak kalmalıdır.

Alternatifler: parser/model içine ad yazmak, eksik kodlara ad tahmin etmek veya
AKD'deki gibi ayrı, isteğe bağlı bir görünüm kullanmak.

Karar: `resolve_live_custodian_brokerages` ve
`resolve_live_custodian_diff_brokerages`, ilgili tek typed yanıtı verilen tek
`LiveBrokerageIndex` ile exact-code olarak ilişkilendirir. Her satırın sayısal
alanları, tarih/aralığı ve request provenance'ı orijinal kaynakta korunur.
`unresolved` ve `ambiguous` kodlara ad atanmaz; görünüm her iki girdiye de
referans tutar.

Sonuç: Günlük takas ve dönem farkı için bellekte, çağrıldığı zaman üretilen
etiketli ara çıktı vardır. Dizin yanıtının tarihsiz oluşu nedeniyle etiketin
ilgili takas tarihinde geçerli olduğu iddia edilmez. `Takas Analizi`, collector,
kalıcı depolama ve LLM context entegrasyonu bu kararın kapsamında değildir;
Takas Analizi için ayrı karar aşağıdadır.

### Canlı Takas Analizi kurum adı bağlantısı

Problem: `custody_analysis_data` içindeki tarihli `akd` ve `custody` serileri
ayrı `broker_code` alanları taşır. Bunları tek seriye toplamak veya kurum adını
parser'a yerleştirmek, farklı veri anlamlarını ve seyrek günleri gizler.

Alternatifler: iki seriyi birleştirmek, adları modele yazmak veya verilen tek
kurum indeksini her seriye ayrı ve isteğe bağlı bir görünüm olarak uygulamak.

Karar: `resolve_live_custody_analysis_brokerages` her `akd` ve `custody` gününü
orijinal sırayla ayrı tutar; her satır için exact-code eşleme sonucunu ekler.
Orijinal yanıt ve kurum indeksi referansları korunur. Eşleşmeyen ve çelişen
kodlara ad atanmaz; sayılar, boş günler, tarih aralığı, metadata ve istek
provenance'ı değiştirilmez.

Sonuç: Bellekte çağrıldığında üretilen bir Takas Analizi görünümü vardır.
Kurum listesinin tarihsiz olması nedeniyle adların tarihsel geçerliliği iddia
edilmez. Collector, kalıcı depolama, legacy snapshot, feature ve LLM context
akışları henüz bu görünümü otomatik kullanmaz.

### Beşli canlı koleksiyon kanıt görünümü

Problem: Aynı kısa collector çağrısındaki brokerage, AKD, günlük Takas, dağılım
ve istatistik yanıtları ayrı typed nesnelerdir. Legacy `MarketSnapshot` bunların
kesirli, nullable ve farklı tarih kapsamlarını kayıpsız temsil etmez.

Alternatifler: canlı yanıtları legacy snapshot'a zorla dönüştürmek, doğrudan LLM
context'e aktarmak veya yalnız mevcut typed nesnelere referans tutan bir ara
görünüm kurmak. İlk iki seçenek doğrulanmamış birim/tarih eşitliği üretir.

Karar: `build_live_collection_evidence`, tek `collect` sonucundan her aile için
tam bir normalize yanıt ister; eksik/çift/pending yanıtı, yerel gönderim
provenance'ı bulunmayan girdiyi, sembol uyuşmazlığını ve günlük Takas'ın istenen
ile dönen tarih farkını reddeder. AKD aralığı, dağılım dönemleri ve istatistik
serileri ayrı korunur; çapraz dönem eşitliği varsayılmaz. Kurum adları yalnız
mevcut exact-code, tarihsiz index üzerinden isteğe bağlı görünümde bulunur;
dağılımın `code` alanı kurum koduna çevrilmez.

Sonuç: Salt-okunur, bellekte kalan bir `LiveCollectionEvidence` vardır. Girdi
`DispatchResult` nesneleri bağlantı kimliği taşımadığı için çağıran tarafın tek
`collect` dönüşünü vermesi gerekir; fonksiyon bunu bağımsız kanıtlayamaz.
Bu görünüm kalıcı kayıt, feature hesabı, LLM context veya finansal doğruluk
onayı değildir.

### Kaydedilmiş dönem paketinin çevrimdışı analizi

Problem: Dönem paketi kaydedildikten sonra AKD mutabakatı, takas farkı
tutarlılığı ve AKD–takas zamanlama eşleşmesi elle ya da model sohbetinde
hesaplanıyordu. Bu hesaplar tekrar üretilemiyor, formülleri kayıtlı değildi ve
her analizde token harcıyordu.

Alternatifler: hesapları model sohbetinde sürdürmek, dönem paketini canlı
collector çıktısı sırasında genişletmek veya kaydedilmiş dosyayı okuyan ayrı,
ağsız bir aşama eklemek. İlk seçenek deterministik değildir; ikincisi canlı
oturumu uzatır ve eski kayıtları kapsamaz.

Karar: `fintr.data.period_capture` kaydı katı biçimde okur, sayıları `Decimal`
olarak korur ve paket hash'ini yeniden hesaplar. `fintr.features.period`
formülleri `period_features_v1` olarak uygular; `fintr.inference.period_report`
bunları sabit sınır metinleriyle JSON rapora çevirir. `scripts/analyze_period.py`
ağ, model veya secret kullanmaz; kaydetme isteğe bağlıdır ve üzerine yazmaz.

Sonuç: Aynı dosya ve parametreler aynı hesapları üretir. Model sohbeti sayıları
yeniden hesaplamak yerine raporu yorumlar. Rapor resmi bir fiyat/hacim serisi
içermez; gecikme eşleşmesi nedensellik veya sağlayıcı yöntemi kanıtı değildir.

#### Ek: günlük piyasa proxy'si (`period_features_v2`)

Problem: Kaydedilmiş dönem paketlerinde fiyat/hacim serisi yok, ama günlük AKD
satırlarında kullanılmayan `total_size`/`total_cost` alanları taşınıyordu;
bunların anlamı doğrulanmamıştı.

Alternatifler: alanları hiç kullanmamak (mevcut durum), doğrulanmamış anlamını
resmi bir fiyat serisiymiş gibi sunmak, veya doğrulanmamış bir hipotez olarak
açıkça etiketleyip iç tutarlılık kontrolleriyle birlikte türetilmiş bir proxy
sunmak. İkinci seçenek AGENTS.md'deki "kaynakta olmayan rakam üretme" kuralını
ihlal eder.

Karar: `fintr.data.period_capture` bu iki alanı da katı biçimde okur;
`fintr.features.period` (`period_features_v2`) bunlardan `derived_proxy_
not_official_ohlc` etiketli bir günlük proxy (`sum_total_size`,
`implied_traded_lots`, `vwap_proxy`) ve dört semantik kontrol (net toplamı
sıfır, toplam lot çift, |net| <= total_size, VWAP aralık içinde) üretir;
eksik güne 0 değil `None` yazılır. `fintr.inference.period_report` bunu ayrı
bir `daily_market_proxy` bölümünde, resmi seri olmadığını belirten bir
kısıtla birlikte taşır.

Sonuç: İki gerçek AKFIS kaydında dört kontrol de her gün geçti (bkz.
`scripts/analyze_period.py --all` çıktısındaki `proxy_checks_passed`), ancak
bu tek sembolün gözlemidir ve hipotezi kanıtlamaz; alan adları veya sağlayıcı
davranışı değişirse kontroller yeniden çalıştırılıp doğrulanmalıdır.

## Veri bölgeleri

- `tests/fixtures/market`: yalnızca sanitize edilmiş küçük protokol örnekleri.
- `data/raw`: gelecekteki değiştirilemez ham veri; Git dışında.
- `data/interim`: ara çıktılar ve geliştirme pilotları; Git dışında.
- `data/processed`: normalize araştırma verisi; Git dışında.
- `data/eval`: erişimi ayrılmış gold evaluation verisi; Git dışında.

Token, cookie, session, kimlik bilgisi, gerçek lisanslı veri, model ağırlığı ve
cache dosyaları Git'e eklenmez.

## Güncel hedef

Öncelik güncel veriyi almak, yanıtları ve yerel gönderilmiş istekleri provenance
kurallarıyla doğrulamak, ardından sınırlı analiz bağlamı hazırlamaktır. Efe bu
kanıtları daha güçlü yönetici modelle birlikte inceler. Repository model
ağırlığı yüklemez, yerel model eğitmez ve doğrudan model API çağrısı yapmaz.

Context builder ve authentication'dan ayrıştırılmış bounded WebSocket collector
offline testlidir; kullanıcı 2026-09-24'te beş aileyi aynı bağlantıda işleyen
ve bellek içi kanıt görünümünü doğrulayan tek bir canlı smoke çıktısı paylaşmıştır.
Ham yanıtlar kalıcı olarak saklanmadı. Diğer request/response varyantları,
kaynak doğruluğu ve lisans/provenance koşulları ayrıca doğrulanmalıdır. Telegram
authentication otomasyonu, bilinmeyen depth/trade request protokolleri ve
fiyat/işlem şeması ayrı mimari karar ister.

AKFIS/2026-09-23 için doğrulanmış beş aileli context yalnız development
pilotudur; prompt/çıktı geliştirmesinde kullanıldığı için held-out evaluation
değildir. İsteğe bağlı capture `data/interim/pilot/` altında kaynak, izin
beyanı, dönüşüm soyu ve checksum metadata'sıyla saklanır; ham frame ve session
bilgisi kaydedilmez. Bağımsız lisans doğrulaması yapılmamıştır. Canlı toplama
ve pilot sınırları [canlı context belgesindedir](live_context.md).
