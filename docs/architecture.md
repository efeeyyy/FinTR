# Architecture draft

## Tasarım ilkeleri

- Belge kanıtı, hesaplanan sonuç ve yorum birbirinden ayrılır.
- Güncel piyasa bağlamı model ağırlığında değil, kaynak ve tarih taşıyan retrieval
  katmanında tutulur.
- Evaluation verisi training ve retrieval corpus'undan fiziksel ve mantıksal
  olarak ayrılır.
- Her ara artefact kaynağına, dönüşüm sürümüne ve checksum'a kadar izlenebilir.
- Bileşenler küçük veri örnekleriyle bağımsız test edilebilir.

## Planlanan akış

```text
Kaynak doküman + metadata
          |
          v
  Alma / doğrulama ------> immutable raw + manifest
          |
          v
 Ayrıştırma / OCR -------> sayfa, tablo, dipnot konumları
          |
          v
 Normalizasyon ----------> birim, para birimi, dönem, şirket kimliği
          |
          +--------------> training/validation hazırlama
          |                       (group-aware split)
          |
          +--------------> retrieval index
          |                       (kaynak + as-of tarihi)
          v
 Prompt / inference ------> iddia + kanıt + belirsizlik
          |
          v
 Evaluation --------------> rubric metrikleri + hata örnekleri
```

## Modül sorumlulukları

- `fintr.data`: şema, manifest, checksum, parsing, normalizasyon ve split kontrolleri
- `fintr.training`: SFT/QLoRA config üretimi, seed ve deney provenance kaydı
- `fintr.inference`: yapılandırılmış prompt/çıktı, alıntı/kanıt bağlama
- `fintr.evaluation`: rubric metrikleri, deterministik kontroller ve raporlama
- `fintr.retrieval`: veri-tarihli BIST/market context ve doküman kanıt erişimi

## Veri bölgeleri

- `data/raw`: değiştirilemez kaynak kopyaları ve manifestler
- `data/interim`: OCR/parsing çıktıları; yeniden üretilebilir
- `data/processed`: training/validation için normalize kayıtlar
- `data/eval`: erişimi ve kullanımı ayrıştırılmış gold evaluation verisi

Gerçek finansal dokümanlar Git'e eklenmemelidir. Büyük artefact ve lisanslı veri
saklama yöntemi, veri toplamadan önce ayrı bir mimari kararla belirlenecektir.

## Henüz verilmemiş kararlar

- Base model ve tokenizer
- Doküman parsing/OCR araç zinciri
- Eğitim işletim sistemi (native Windows veya WSL2/Linux)
- Veri lisanslama ve saklama altyapısı
- Retrieval store ve embedding modeli
- Kesin bağımlılık sürümleri ve lockfile biçimi

Bu kararlar benchmark, lisans, Türkçe performansı ve 8 GiB VRAM bellek bütçesi
görülmeden sabitlenmeyecektir.

