# FinTR-LLM architecture

## Tasarım ilkeleri

- Ham piyasa verisi, normalize kayıt, deterministik özellik ve LLM yorumu ayrı
  katmanlardır.
- Sembol, tarih/zaman ve mesaj türü dönüşüm boyunca korunur.
- Parser'lar eksik alanı veya yanlış tipi sessizce düzeltmez.
- AKD ve takas farklı zaman/kapsam semantiğine sahip veri aileleridir.
- Evaluation verisi gelecekteki training verisinden fiziksel ve mantıksal olarak
  ayrı tutulur.
- Mevcut quantized Qwen inference katmanı veri toplama ve hesap katmanından
  bağımsız kalır.

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
       LLM inference ----------> Türkçe açıklama + koşullu muhakeme
            |
            v
        evaluation ------------> rubric metrikleri + failure örnekleri
```

Bounded WebSocket collector uygulanmıştır. Kullanıcının canlı denemelerinde
handshake başarılıdır; ancak `get_brokerages` için beklenen response henüz
alınamamıştır. `tests/fixtures/market/` altındaki sanitize mesajlar offline
sözleşmelerdir; canlı sağlayıcı şemasının doğrulandığını göstermez. Son gözlem ve
public JS ile brokerage parser uyuşmazlığı için [collector sınırları](collector.md)
belgesine bakın.

## Modül sorumlulukları

- `fintr.data.models`: normalize ve immutable piyasa veri sözleşmeleri.
- `fintr.data.parsers`: mesaj türüne özgü doğrulama ve normalize etme.
- `fintr.collector`: runtime secret provider, bounded WebSocket/replay transport,
  event dispatcher, request builders ve freshness-aware snapshot assembler.
- `fintr.features`: LLM'den bağımsız sayısal özellik hesapları.
- `fintr.inference.context`: sınırlı, provenance taşıyan structured context ve
  deterministik text renderer.
- `fintr.inference.prompts`: model çağrısından bağımsız analysis prompt şablonu.
- `fintr.inference.local`: mevcut model yükleme ve metin üretimi; değişmeden kalır.
- `fintr.evaluation`: piyasa mikroyapısı rubric'i ve deterministik kontroller.
- `fintr.training` / `fintr.retrieval`: sonraki aşamalar; bu çalışmada devre dışı.

## Veri modelleri

Fiyat, maliyet ve yüzdeler `Decimal`; lot, değer ve yatırımcı adedi `int`; tarih
alanları `date`/`datetime` olarak normalize edilir. Koleksiyonlar immutable
`tuple` biçimindedir. `MarketSnapshot`, aynı sembole ait opsiyonel veri ailelerini
bir araya getirir; tarih/kapsam uyumunu context builder ayrıca doğrulayacaktır.

## Veri bölgeleri

- `tests/fixtures/market`: yalnızca sanitize edilmiş küçük protokol örnekleri.
- `data/raw`: gelecekteki değiştirilemez ham veri; Git dışında.
- `data/interim`: yeniden üretilebilir parsing çıktıları; Git dışında.
- `data/processed`: normalize araştırma verisi; Git dışında.
- `data/eval`: erişimi ayrılmış gold evaluation verisi; Git dışında.

Token, cookie, session, kimlik bilgisi, gerçek lisanslı veri, model ağırlığı ve
cache dosyaları Git'e eklenmez.

## Sonraki mimari dilim

Context builder ve authentication'dan ayrıştırılmış bounded WebSocket collector
offline testlidir; canlı provider entegrasyonu tamamlanmış değildir. Öncelikli
doğrulama eşiği request/response akışını ve gerçek response şemasını kanıtlamak,
fixture/parser uyuşmazlığını gidermek ve kaynak doğruluğu ile lisans/provenance
koşullarını netleştirmektir. Bu provider yolu persistent collection ve
dataset/training için hazır değildir. Persistent storage, retention ve provenance
manifest tasarımı sonraki dilimdir; tasarım yapılması canlı toplamaya hazır
olunduğu anlamına gelmez. Telegram authentication otomasyonu, bilinmeyen
depth/trade request protokolleri ve fiyat/işlem şeması ayrı mimari karar ister.
